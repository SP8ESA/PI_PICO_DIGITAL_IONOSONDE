#pragma once
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// --- API LUT ---
void bpsk_gen_lut(int lut_size, int interp);

// --- API konfiguracji nadajnika ---
// base_pin: pierwszy pin R-2R (np. 8), bits: liczba bitów (np. 8)
// transition_us: szerokość okna "soft phase" (centrowane na granicy bitów)
// fade_us: fade-in/out (nie zmienia długości chipa)
// gp8_is_msb: 1 jeśli GP8 to MSB (odwracamy bity bajtu)
void bpsk_config(int base_pin, int bits, float transition_us, float fade_us, int gp8_is_msb);

// --- Sterowanie pinami (zdarzenia kontroli) ---
typedef enum {
    BPSK_REF_START = 0,   // offset względem początku chipa
    BPSK_REF_END   = 1    // offset względem końca chipa (koniec CZĘŚCI DANYCH, bez ogona)
} bpsk_ref_t;

typedef struct {
    uint32_t    pin;         // GPIO nr
    bool        level;       // 0=LOW, 1=HIGH
    int32_t     offset_us;   // opóźnienie w us (może być ujemne)
    bpsk_ref_t  ref;         // START lub END
} bpsk_ctrl_event_t;

// --- Transmisja ramki (blokująco) ---
// seq_str: "CARRIER", "BARKER2"..."BARKER13" albo własny "101001"
// bit_us:  czas bitu [us]
// freq_hz: nośna [Hz]
// amplitude: 0..1
// chip_start_to_start_us: odstęp start->start między chipami [us]
// chip_count: ile chipów nadać
// ctrl: tablica zdarzeń sterowania GPIO (może być NULL), ctrl_count: liczba zdarzeń (0..N)
// Zwraca true przy sukcesie.
bool bpsk_transmit(const char* seq_str,
                   float bit_us,
                   double freq_hz,
                   float amplitude,
                   uint32_t chip_start_to_start_us,
                   int chip_count,
                   const bpsk_ctrl_event_t* ctrl,
                   int ctrl_count);

#ifdef __cplusplus
}
#endif
