#include "bpsk_tx.h"

#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "pico/stdlib.h"
#include "hardware/pio.h"
#include "hardware/dma.h"
#include "hardware/clocks.h"
#include "hardware/gpio.h"

// ====== Parametry/modułowe zmienne ======
static PIO   s_pio = pio0;
static uint  s_sm  = 0;
static int   s_base_pin = 8;
static int   s_bits     = 8;
static int   s_gp8_is_msb = 1;

static float s_transition_us = 2.5f;
static float s_fade_us       = 2.5f;

static int   s_lut_size = 1440;
static int   s_interp   = 0;

static int16_t *s_sine_lut = NULL; // [s_lut_size+1]
static int      s_dma_ch = -1;

static uint32_t s_pins_mask;
static uint32_t s_pin_values_mid; // 0x80 po mapowaniu

// Bufor TX (pakowany 4x8b -> 32b)
static uint32_t *s_words = NULL;
static int       s_words_count = 0;

// ====== Utils ======
static inline uint8_t rev8(uint8_t x){
    x = (uint8_t)(((x & 0xF0) >> 4) | ((x & 0x0F) << 4));
    x = (uint8_t)(((x & 0xCC) >> 2) | ((x & 0x33) << 2));
    x = (uint8_t)(((x & 0xAA) >> 1) | ((x & 0x55) << 1));
    return x;
}
static inline uint8_t map_sample(uint8_t v){
    return s_gp8_is_msb ? rev8(v) : v;
}
static inline void gpio_fast_drive(int pin){
    gpio_set_slew_rate(pin, GPIO_SLEW_RATE_FAST);
    gpio_set_drive_strength(pin, GPIO_DRIVE_STRENGTH_12MA);
}
static void ensure_pin_output(uint pin){
    static uint8_t inited[32] = {0};
    if (pin < 32 && !inited[pin]) {
        gpio_init(pin);
        gpio_set_dir(pin, GPIO_OUT);
        gpio_fast_drive(pin);
        inited[pin] = 1;
    }
}

// ====== LUT ======
void bpsk_gen_lut(int lut_size, int interp){
    if (lut_size < 16) lut_size = 16;
    s_lut_size = lut_size;
    s_interp   = (interp != 0);

    if (s_sine_lut) { free(s_sine_lut); s_sine_lut = NULL; }
    s_sine_lut = (int16_t*)malloc((size_t)(s_lut_size + 1) * sizeof(int16_t));
    for (int i = 0; i <= s_lut_size; ++i){
        double th = (2.0 * M_PI * i) / (double)s_lut_size;
        long v = lround(32767.0 * sin(th));
        if (v < -32767) v = -32767; if (v > 32767) v = 32767;
        s_sine_lut[i] = (int16_t)v;
    }
}

static inline int16_t lut_sample_q32(uint64_t phase_fp){
    uint32_t idx  = (uint32_t)(phase_fp >> 32) % s_lut_size;
    if (!s_interp) return s_sine_lut[idx];
    uint32_t frac = (uint32_t)phase_fp;
    int16_t y0 = s_sine_lut[idx];
    int16_t y1 = s_sine_lut[idx+1];
    int32_t dy = (int32_t)y1 - (int32_t)y0;
    int32_t interp = (int32_t)y0 + (int32_t)(((int64_t)dy * (int64_t)frac) >> 32);
    return (int16_t)interp;
}

// ====== PIO program (1 instrukcja: OUT PINS, s_bits) ======
static void pio_prepare(void){
    static uint16_t prog[1];
    static struct pio_program prg;
    prog[0] = pio_encode_out(pio_pins, s_bits);
    prg.instructions = prog; prg.length = 1; prg.origin = -1;

    uint off = pio_add_program(s_pio, &prg);
    s_sm = pio_claim_unused_sm(s_pio, true);

    pio_sm_config c = pio_get_default_sm_config();
    sm_config_set_out_pins(&c, s_base_pin, s_bits);
    sm_config_set_set_pins(&c, s_base_pin, s_bits);
    sm_config_set_out_shift(&c, true, true, 32);
    sm_config_set_wrap(&c, off, off);
    sm_config_set_fifo_join(&c, PIO_FIFO_JOIN_TX);
    sm_config_set_clkdiv(&c, 1.0f);

    pio_sm_init(s_pio, s_sm, off, &c);

    s_pins_mask      = ((1u << s_bits) - 1u) << s_base_pin;
    s_pin_values_mid = ((uint32_t)map_sample(0x80)) << s_base_pin;

    for (int p = s_base_pin; p < s_base_pin + s_bits; ++p){
        pio_gpio_init(s_pio, p);
        gpio_fast_drive(p);
    }
    pio_sm_set_pins_with_mask(s_pio, s_sm, s_pin_values_mid, s_pins_mask);
    pio_sm_set_pindirs_with_mask(s_pio, s_sm, s_pins_mask, s_pins_mask);
}

void bpsk_config(int base_pin, int bits, float transition_us, float fade_us, int gp8_is_msb){
    s_base_pin = base_pin;
    s_bits     = bits;
    if (s_bits < 1) s_bits = 1; if (s_bits > 16) s_bits = 16;
    s_gp8_is_msb = gp8_is_msb ? 1 : 0;
    s_transition_us = transition_us;
    s_fade_us       = fade_us;

    pio_prepare();

    if (s_dma_ch < 0) {
        s_dma_ch = dma_claim_unused_channel(true);
    }
    dma_channel_config cc = dma_channel_get_default_config(s_dma_ch);
    channel_config_set_transfer_data_size(&cc, DMA_SIZE_32);
    channel_config_set_read_increment(&cc, true);
    channel_config_set_write_increment(&cc, false);
    channel_config_set_dreq(&cc, pio_get_dreq(s_pio, s_sm, true));
    dma_channel_configure(s_dma_ch, &cc, &s_pio->txf[s_sm], NULL, 0, false);

    pio_sm_set_pins_with_mask(s_pio, s_sm, s_pin_values_mid, s_pins_mask);
}

// ====== Sekwencje ======
static bool parse_sequence(const char* seq_str, uint8_t **out_bits, int *out_len){
    static const struct { const char* name; const char* bits; } barker[] = {
        {"BARKER2",  "10"},
        {"BARKER3",  "110"},
        {"BARKER4",  "1110"},
        {"BARKER5",  "11101"},
        {"BARKER7",  "1110010"},
        {"BARKER11", "11100010010"},
        {"BARKER13", "1111100110101"},
    };
    *out_bits = NULL; *out_len = 0;

    if (0 == strcasecmp(seq_str, "CARRIER")) {
        *out_len = 1;
        *out_bits = (uint8_t*)malloc(1);
        (*out_bits)[0] = 1;
        return true;
    }
    for (size_t i=0;i<sizeof(barker)/sizeof(barker[0]);++i){
        if (0 == strcasecmp(seq_str, barker[i].name)){
            int n = (int)strlen(barker[i].bits);
            uint8_t *v = (uint8_t*)malloc((size_t)n);
            for (int k=0;k<n;++k) v[k] = (barker[i].bits[k] == '1') ? 1 : 0;
            *out_bits = v; *out_len = n;
            return true;
        }
    }
    // własny ciąg '0'/'1'
    int n = (int)strlen(seq_str);
    if (n<=0) return false;
    uint8_t *v = (uint8_t*)malloc((size_t)n);
    for (int k=0;k<n;++k){
        char c = seq_str[k];
        if (c!='0' && c!='1'){ free(v); return false; }
        v[k] = (c=='1') ? 1 : 0;
    }
    *out_bits = v; *out_len = n;
    return true;
}

// DDS step (Q32.32)
static inline uint64_t dds_step_q32(double f_carrier, double Fs, int lut_size){
    double step = (f_carrier / Fs) * (double)lut_size;
    if (step <= 0) step = 1.0 / (double)(1ull<<32);
    uint64_t s = (uint64_t) llround(step * (double)(1ull<<32));
    if (s == 0) s = 1;
    return s;
}

// Precompute: fade Hann (Q7) i okno RC pełne (Q8)
static uint8_t* make_hann_q7(int fade_samples, int gain_max){
    if (fade_samples < 1) fade_samples = 1;
    uint8_t *t = (uint8_t*)malloc((size_t)fade_samples);
    for (int i=0;i<fade_samples;++i){
        double a = 0.5 * (1.0 - cos(M_PI * ((double)i / (double)fade_samples)));
        long v = lround((double)gain_max * a);
        if (v<0) v=0; if (v>127) v=127;
        t[i] = (uint8_t)v;
    }
    return t;
}
static uint8_t* make_rc_q8(int L){
    if (L < 2) L = 2;
    uint8_t *t = (uint8_t*)malloc((size_t)L);
    for (int k=0;k<L;++k){
        double u = (double)k / (double)(L-1);
        double a = 0.5 * (1.0 - cos(M_PI * u));
        long v = lround(255.0 * a);
        if (v<0) v=0; if (v>255) v=255;
        t[k] = (uint8_t)v;
    }
    return t;
}

// Zbuduj bufor pojedynczego chipa (stała długość, centrowane przejścia).
// Zwraca liczbę próbek danych (bez ogona), zapisuje s_words/s_words_count.
static uint64_t build_chip_buffer(const uint8_t* bits, int NB,
                                  int Nbit, int Ltr, int Lfade,
                                  double freq_hz, float amplitude){
    const double Fs = (double)clock_get_hz(clk_sys);
    const uint64_t step_q32      = dds_step_q32(freq_hz, Fs, s_lut_size);
    const uint64_t HALF_TURN_Q32 = ((uint64_t)(s_lut_size/2) << 32);

    if (Ltr < 2) Ltr = 2;
    int Lh = Ltr / 2;
    if (Lfade < 1) Lfade = 1;

    int gain_max = (int)lroundf(127.0f * (amplitude < 0 ? 0 : (amplitude > 1 ? 1 : amplitude)));
    uint8_t *hann_q7 = make_hann_q7(Lfade, gain_max);
    uint8_t *rc_q8   = make_rc_q8(Ltr);

    uint64_t total_data_samples = (uint64_t)NB * (uint64_t)Nbit;
    const int TAIL = 64; // ogon na środek między chipami
    uint64_t total_samples = total_data_samples + TAIL;

    uint64_t padded = (total_samples + 3) & ~3ull;
    int words_needed = (int)(padded / 4);
    if (s_words) { free(s_words); s_words = NULL; }
    s_words = (uint32_t*)malloc((size_t)words_needed * sizeof(uint32_t));
    s_words_count = words_needed;

    uint64_t phase = 0;
    uint64_t s_idx = 0;
    int out_word_idx = 0, byte_in_word = 0;
    uint32_t cur_word = 0;

    for (uint64_t s = 0; s < total_data_samples; ++s){
        phase += step_q32;

        int bit_idx = (int)(s / Nbit);
        uint64_t phi_prev = (bit_idx > 0            && bits[bit_idx-1] ? 0 : HALF_TURN_Q32);
        uint64_t phi_cur  = (bits[bit_idx]          ? 0 : HALF_TURN_Q32);
        uint64_t phi_next = (bit_idx < NB - 1       && bits[bit_idx+1] ? 0 : HALF_TURN_Q32);

        uint64_t off_q32 = phi_cur;
        bool blended = false;

        if (bit_idx > 0 && (bits[bit_idx-1] != bits[bit_idx])) {
            int64_t center_prev = (int64_t)bit_idx * (int64_t)Nbit;
            int64_t dist = (int64_t)s - center_prev;
            if (dist >= -(int64_t)Lh && dist < (int64_t)Lh) {
                int k = (int)(dist + Lh);
                uint8_t a8 = rc_q8[k];
                int64_t delta = (int64_t)phi_cur - (int64_t)phi_prev;
                off_q32 = (uint64_t)((int64_t)phi_prev + ((delta * (int64_t)a8) >> 8));
                blended = true;
            }
        }
        if (!blended && bit_idx < NB - 1 && (bits[bit_idx] != bits[bit_idx+1])) {
            int64_t center_next = (int64_t)(bit_idx + 1) * (int64_t)Nbit;
            int64_t dist = (int64_t)s - center_next;
            if (dist >= -(int64_t)Lh && dist < (int64_t)Lh) {
                int k = (int)(dist + Lh);
                uint8_t a8 = rc_q8[k];
                int64_t delta = (int64_t)phi_next - (int64_t)phi_cur;
                off_q32 = (uint64_t)((int64_t)phi_cur + ((delta * (int64_t)a8) >> 8));
                blended = true;
            }
        }

        int16_t y = lut_sample_q32(phase + off_q32);

        int gain = gain_max;
        if (s_idx < (uint64_t)Lfade) {
            gain = hann_q7[(int)s_idx];
        } else if (s_idx >= total_data_samples - (uint64_t)Lfade) {
            uint64_t ii = total_data_samples - 1 - s_idx;
            if (ii >= (uint64_t)Lfade) ii = Lfade - 1;
            gain = hann_q7[(int)ii];
        }

        int32_t s8 = ((int32_t)gain * (int32_t)y) >> 15; // ±127
        int val = 0x80 + s8;
        if (val < 0) val = 0; if (val > 255) val = 255;
        uint8_t out8 = map_sample((uint8_t)val);

        cur_word |= ((uint32_t)out8) << (8*byte_in_word);
        if (++byte_in_word == 4){ s_words[out_word_idx++] = cur_word; cur_word = 0; byte_in_word = 0; }

        s_idx++;
    }

    // ogon środka
    for (int t=0;t<TAIL;++t){
        uint8_t out8 = map_sample(0x80);
        cur_word |= ((uint32_t)out8) << (8*byte_in_word);
        if (++byte_in_word == 4){ s_words[out_word_idx++] = cur_word; cur_word = 0; byte_in_word = 0; }
    }
    while (byte_in_word != 0){
        uint8_t out8 = map_sample(0x80);
        cur_word |= ((uint32_t)out8) << (8*byte_in_word);
        if (++byte_in_word == 4){ s_words[out_word_idx++] = cur_word; cur_word = 0; byte_in_word = 0; }
    }

    free(hann_q7);
    free(rc_q8);
    return total_data_samples; // próbki DANYCH (bez ogona)
}

// Sortowalny "snapshot" zdarzenia z czasem absolutnym
typedef struct {
    absolute_time_t t_abs;
    uint32_t pin;
    bool level;
} ev_abs_t;

static int cmp_ev_abs(const void* a, const void* b){
    const ev_abs_t* A = (const ev_abs_t*)a;
    const ev_abs_t* B = (const ev_abs_t*)b;
    // rosnąco: wcześniej -> wcześniej w tablicy
    int64_t d = absolute_time_diff_us(A->t_abs, B->t_abs); // = B - A
    if (d > 0) return -1;   // A jest wcześniej niż B
    if (d < 0) return  1;   // A jest później niż B
    return 0;
}

// ===== FIX: rebase czasu, jeśli najwcześniejsze zdarzenie wypadło już w przeszłości
static void rebase_if_behind(absolute_time_t *t_start,
                             absolute_time_t *t_end_data,
                             ev_abs_t *ev, int n_ev)
{
    if (!ev || n_ev <= 0) return;

    absolute_time_t now = get_absolute_time();

    // Szukamy najpierw najwcześniejszego PRE (t_abs <= t_start),
    // a jeśli go nie ma – najwcześniejszego czegokolwiek.
    bool have_pre = false;
    absolute_time_t earliest = *t_start;
    uint64_t us_start = to_us_since_boot(*t_start);

    for (int i = 0; i < n_ev; ++i) {
        uint64_t us_i = to_us_since_boot(ev[i].t_abs);
        if (us_i <= us_start) {
            if (!have_pre || us_i < to_us_since_boot(earliest)) earliest = ev[i].t_abs;
            have_pre = true;
        }
    }
    if (!have_pre) {
        for (int i = 0; i < n_ev; ++i) {
            if (to_us_since_boot(ev[i].t_abs) < to_us_since_boot(earliest)) earliest = ev[i].t_abs;
        }
    }

    // Ile jesteśmy "spóźnieni"?
    int64_t lag_us = (int64_t)to_us_since_boot(now) - (int64_t)to_us_since_boot(earliest);
    if (lag_us > 0) {
        const int64_t SAFETY_US = 20; // mały zapas
        int64_t shift = lag_us + SAFETY_US;

        *t_start    = delayed_by_us(*t_start,    shift);
        *t_end_data = delayed_by_us(*t_end_data, shift);
        for (int i = 0; i < n_ev; ++i) {
            ev[i].t_abs = delayed_by_us(ev[i].t_abs, shift);
        }
    }
}


// ====== TX ramki z S2S i zdarzeniami GPIO ======
bool bpsk_transmit(const char* seq_str,
                   float bit_us,
                   double freq_hz,
                   float amplitude,
                   uint32_t chip_start_to_start_us,
                   int chip_count,
                   const bpsk_ctrl_event_t* ctrl,
                   int ctrl_count)
{
    if (!s_sine_lut || chip_count <= 0) return false;

    uint8_t *bits = NULL; int NB = 0;
    if (!parse_sequence(seq_str, &bits, &NB)) return false;

    const double Fs = (double)clock_get_hz(clk_sys);
    int Nbit = (int)lroundf(bit_us * (float)Fs / 1e6f);
    if (Nbit < 8) Nbit = 8;

    int Ltr   = (int)lroundf(s_transition_us * (float)Fs / 1e6f); if (Ltr < 2) Ltr = 2;
    int Lfade = (int)lroundf(s_fade_us       * (float)Fs / 1e6f); if (Lfade < 1) Lfade = 1;

    // Bufor jednego chipa i jego czas (danych)
    uint64_t data_samples = build_chip_buffer(bits, NB, Nbit, Ltr, Lfade, freq_hz, amplitude);
    const uint32_t chip_data_us = (uint32_t)((data_samples * 1000000ull) / (uint64_t)clock_get_hz(clk_sys));

    // Zainicjalizuj piny sterujące (raz)
    for (int i=0;i<ctrl_count;++i){
        ensure_pin_output(ctrl[i].pin);
    }

    // Ramka: chip_count chipów z S2S
    for (int i = 0; i < chip_count; ++i){
        absolute_time_t t_start = delayed_by_us(get_absolute_time(), 150);  // zaplanuj chip za 150 us
        absolute_time_t t_end_data = delayed_by_us(t_start, chip_data_us);

        // Zbuduj listę zdarzeń absolutnych dla TEGO chipa
        ev_abs_t *ev = NULL;
        int n_ev = ctrl_count;
        if (n_ev > 0) {
            ev = (ev_abs_t*)malloc((size_t)n_ev * sizeof(ev_abs_t));
            for (int k=0;k<n_ev;++k){
                absolute_time_t tref = (ctrl[k].ref == BPSK_REF_END) ? t_end_data : t_start;
                ev[k].t_abs = delayed_by_us(tref, ctrl[k].offset_us);
                ev[k].pin   = ctrl[k].pin;
                ev[k].level = ctrl[k].level;
            }
            qsort(ev, (size_t)n_ev, sizeof(ev_abs_t), cmp_ev_abs);
            rebase_if_behind(&t_start, &t_end_data, ev, n_ev);   // <<< DODAJ TO

            
        }

        // --- zdarzenia PRE (<= t_start) ---
        for (int k = 0; k < n_ev; ++k) {
            if (to_us_since_boot(ev[k].t_abs) <= to_us_since_boot(t_start)) {
                // czekaj tylko jeśli to w przyszłości; jeśli już minęło – wykonaj od razu
                if (absolute_time_diff_us(get_absolute_time(), ev[k].t_abs) > 0) {
                    busy_wait_until(ev[k].t_abs);
                }
                gpio_put(ev[k].pin, ev[k].level);
            }
        }

// wyrównanie dokładnie do t_start
        if (absolute_time_diff_us(get_absolute_time(), t_start) > 0) {
            busy_wait_until(t_start);
        }

// --- START DMA + PIO (początek chipa) ---
pio_sm_set_pins_with_mask(s_pio, s_sm, s_pin_values_mid, s_pins_mask);
dma_channel_set_read_addr(s_dma_ch, s_words, false);
dma_channel_set_trans_count(s_dma_ch, s_words_count, false);
dma_channel_start(s_dma_ch);
pio_sm_set_enabled(s_pio, s_sm, true);

// --- zdarzenia POST (> t_start) wykonywane w trakcie DMA ---
        for (int k = 0; k < n_ev; ++k) {
            if (to_us_since_boot(ev[k].t_abs) > to_us_since_boot(t_start)) {
                if (absolute_time_diff_us(get_absolute_time(), ev[k].t_abs) > 0) {
                   busy_wait_until(ev[k].t_abs);
                }
                gpio_put(ev[k].pin, ev[k].level);
            }
        }

        // Poczekaj aż DMA skończy (może trwać chwilę po t_end_data z powodu ogona)
        while (dma_channel_is_busy(s_dma_ch)) { tight_loop_contents(); }

        // Stop i środek
        pio_sm_set_enabled(s_pio, s_sm, false);
        pio_sm_set_pins_with_mask(s_pio, s_sm, s_pin_values_mid, s_pins_mask);

        // S2S: od startu bieżącego do startu następnego
        absolute_time_t t_next = delayed_by_us(t_start, (int64_t)chip_start_to_start_us);
        busy_wait_until(t_next);

        if (ev) free(ev);
    }

    free(bits);
    return true;
}
