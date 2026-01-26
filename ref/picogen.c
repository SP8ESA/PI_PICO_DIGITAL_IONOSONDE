#include <stdio.h>
#include "pico/stdlib.h"
#include "hardware/clocks.h"
#include "tusb.h"
#include "pico/stdio_usb.h"

#include "bpsk_tx.h"

#define OVERCLOCK_KHZ             250000

// Piny sterujące – dopasuj do swojego HW
#define PIN_RX_EN   2   // odbiornik enable (1=ON)
#define PIN_SW_TX   3   // przełącznik T/R: 1=TX, 0=RX
#define PIN_PA_EN   4   // wzmacniacz mocy: 1=ON

static void wait_usb(void){
    absolute_time_t t0 = make_timeout_time_ms(1500);
    while (absolute_time_diff_us(get_absolute_time(), t0) > 0) {
        if (tud_cdc_connected() || stdio_usb_connected()) break;
        tight_loop_contents();
    }
}

int main(){
    set_sys_clock_khz(OVERCLOCK_KHZ, true);
    stdio_init_all();
    wait_usb();
    printf("[BOOT] clk_sys=%.3f MHz\n", clock_get_hz(clk_sys)/1e6f);

    // Inicjalizacja LUT i R-2R
    bpsk_gen_lut(1440, 0);
    bpsk_config(/*base_pin*/8, /*bits*/8,
                /*transition_us*/2.5f, /*fade_us*/2.5f,
                /*gp8_is_msb*/1);

    // Zdarzenia sterujące dla KAŻDEGO chipa:
    // kolejność logiczna: wyłącz RX -> przełącz na TX -> włącz PA -> (chip) -> wyłącz PA -> na RX -> włącz RX
    // Przykładowe offsety (dostosuj do swojego PA/dupleksera):
    //  -200 us: RX_EN=0
    //  -150 us: SW_TX=1
    //  -100 us: PA_EN=1
    //  +  10 us od END: PA_EN=0
    //  +  40 us od END: SW_TX=0
    //  +  60 us od END: RX_EN=1
    const bpsk_ctrl_event_t CTRL_SEQ[] = {
        { PIN_RX_EN, 0,  -2, BPSK_REF_START },
        { PIN_SW_TX, 1,  -1, BPSK_REF_START },
        { PIN_PA_EN, 1,  0, BPSK_REF_START },
        { PIN_PA_EN, 0,   3, BPSK_REF_END   },
        { PIN_SW_TX, 0,   4, BPSK_REF_END   },
        { PIN_RX_EN, 1,   5, BPSK_REF_END   },
    };
    const int CTRL_COUNT = sizeof(CTRL_SEQ)/sizeof(CTRL_SEQ[0]);

    // Wspólne parametry chipów
    const float   bit_us  = 40.0f;
    const double  freq_hz = 7022000.0;
    const uint32_t chip_s2s_us = 4975;
    const int     chip_count   = 2048;

    // Harmonogram trzech ramek co 1 s (start-to-start)
    absolute_time_t t_sched = get_absolute_time();

    while (true) {
        // 1) Barker-13
        sleep_until(t_sched);
        bpsk_transmit("BARKER13", bit_us, freq_hz, 0.8f, chip_s2s_us, chip_count, CTRL_SEQ, CTRL_COUNT);
        t_sched = delayed_by_ms(t_sched, 20000);


    }
}
