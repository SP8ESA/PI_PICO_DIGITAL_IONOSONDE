/**
 * BPSK Ionosonde - Raspberry Pi Pico
 * 
 * Sterowanie przez USB Serial:
 * - TX_ONCE      -> nadaj jedną ramkę
 * - TX_AUTO      -> nadawaj w pętli
 * - TX_STOP      -> zatrzymaj
 * - SET FREQ=x,BIT=x,AMP=x,MOD=x,CHIPS=x,S2S=x,INTERVAL=x
 * - STATUS       -> pokaż parametry
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "pico/stdlib.h"
#include "hardware/clocks.h"
#include "pico/stdio_usb.h"

#include "bpsk_tx.h"

// ====== Konfiguracja sprzętu ======
#define OVERCLOCK_KHZ   250000
#define PIN_RX_EN       2
#define PIN_SW_TX       3
#define PIN_PA_EN       4

// ====== Parametry TX (modyfikowalne) ======
static double  g_freq_hz      = 7022000.0;
static float   g_bit_us       = 40.0f;
static float   g_amplitude    = 0.85f;
static char    g_modulation[16] = "BARKER13";
static int     g_chip_count   = 2048;
static int     g_chip_s2s_us  = 4975;
static int     g_interval_ms  = 20000;

// Stan TX
static bool    g_tx_auto      = false;
static int     g_frame_number = 0;

// GPIO control events
static bpsk_ctrl_event_t g_ctrl_seq[] = {
    { PIN_RX_EN,  0,    -200, BPSK_REF_START },
    { PIN_SW_TX,  1,    -150, BPSK_REF_START },
    { PIN_PA_EN,  1,    -100, BPSK_REF_START },
    { PIN_PA_EN,  0,     +10, BPSK_REF_END   },
    { PIN_SW_TX,  0,     +40, BPSK_REF_END   },
    { PIN_RX_EN,  1,     +60, BPSK_REF_END   },
};
#define CTRL_COUNT (sizeof(g_ctrl_seq) / sizeof(g_ctrl_seq[0]))

// ====== Funkcje ======

static void init_control_pins(void) {
    gpio_init(PIN_RX_EN); gpio_set_dir(PIN_RX_EN, GPIO_OUT); gpio_put(PIN_RX_EN, 1);
    gpio_init(PIN_SW_TX); gpio_set_dir(PIN_SW_TX, GPIO_OUT); gpio_put(PIN_SW_TX, 0);
    gpio_init(PIN_PA_EN); gpio_set_dir(PIN_PA_EN, GPIO_OUT); gpio_put(PIN_PA_EN, 0);
}

static void print_status(void) {
    printf("\n=== Ionosonde Status ===\n");
    printf("Frequency: %.6f MHz\n", g_freq_hz / 1e6);
    printf("Bit Duration: %.1f us\n", g_bit_us);
    printf("Amplitude: %.0f%%\n", g_amplitude * 100);
    printf("Modulation: %s\n", g_modulation);
    printf("Chip Count: %d\n", g_chip_count);
    printf("Chip S2S: %d us\n", g_chip_s2s_us);
    printf("Frame Interval: %d ms\n", g_interval_ms);
    printf("TX Auto: %s\n", g_tx_auto ? "ON" : "OFF");
    printf("Frame Count: %d\n", g_frame_number);
    printf("--- GPIO Timing ---\n");
    printf("RX_OFF: %d us (PRE)\n", g_ctrl_seq[0].offset_us);
    printf("TX_ON:  %d us (PRE)\n", g_ctrl_seq[1].offset_us);
    printf("PA_ON:  %d us (PRE)\n", g_ctrl_seq[2].offset_us);
    printf("PA_OFF: %d us (POST)\n", g_ctrl_seq[3].offset_us);
    printf("TX_OFF: %d us (POST)\n", g_ctrl_seq[4].offset_us);
    printf("RX_ON:  %d us (POST)\n", g_ctrl_seq[5].offset_us);
    printf("========================\n\n");
}

static void do_transmit(void) {
    printf("[TX-%d] Started at %llu us\n", g_frame_number, to_us_since_boot(get_absolute_time()));
    
    bool ok = bpsk_transmit(
        g_modulation,
        g_bit_us,
        g_freq_hz,
        g_amplitude,
        g_chip_s2s_us,
        g_chip_count,
        g_ctrl_seq,
        CTRL_COUNT
    );
    
    if (ok) {
        printf("[TX-%d] Completed\n", g_frame_number);
    } else {
        printf("[TX-%d] ERROR!\n", g_frame_number);
    }
    g_frame_number++;
}

static void parse_set_command(char *params) {
    // Format: FREQ=7.022,BIT=40,AMP=0.85,MOD=BARKER13,CHIPS=2048,S2S=4975,INTERVAL=20000
    char *token = strtok(params, ",");
    while (token) {
        char *eq = strchr(token, '=');
        if (eq) {
            *eq = '\0';
            char *key = token;
            char *val = eq + 1;
            
            if (strcmp(key, "FREQ") == 0) {
                g_freq_hz = atof(val) * 1e6;
                printf("[SET] Frequency: %.6f MHz\n", g_freq_hz / 1e6);
            } else if (strcmp(key, "BIT") == 0) {
                g_bit_us = atof(val);
                printf("[SET] Bit Duration: %.1f us\n", g_bit_us);
            } else if (strcmp(key, "AMP") == 0) {
                g_amplitude = atof(val);
                printf("[SET] Amplitude: %.0f%%\n", g_amplitude * 100);
            } else if (strcmp(key, "MOD") == 0) {
                strncpy(g_modulation, val, sizeof(g_modulation) - 1);
                printf("[SET] Modulation: %s\n", g_modulation);
            } else if (strcmp(key, "CHIPS") == 0) {
                g_chip_count = atoi(val);
                printf("[SET] Chip Count: %d\n", g_chip_count);
            } else if (strcmp(key, "S2S") == 0) {
                g_chip_s2s_us = atoi(val);
                printf("[SET] Chip S2S: %d us\n", g_chip_s2s_us);
            } else if (strcmp(key, "INTERVAL") == 0) {
                g_interval_ms = atoi(val);
                printf("[SET] Frame Interval: %d ms\n", g_interval_ms);
            } else if (strcmp(key, "RX_OFF") == 0) {
                g_ctrl_seq[0].offset_us = atoi(val);
                printf("[SET] RX_OFF offset: %d us\n", g_ctrl_seq[0].offset_us);
            } else if (strcmp(key, "TX_ON") == 0) {
                g_ctrl_seq[1].offset_us = atoi(val);
                printf("[SET] TX_ON offset: %d us\n", g_ctrl_seq[1].offset_us);
            } else if (strcmp(key, "PA_ON") == 0) {
                g_ctrl_seq[2].offset_us = atoi(val);
                printf("[SET] PA_ON offset: %d us\n", g_ctrl_seq[2].offset_us);
            } else if (strcmp(key, "PA_OFF") == 0) {
                g_ctrl_seq[3].offset_us = atoi(val);
                printf("[SET] PA_OFF offset: %d us\n", g_ctrl_seq[3].offset_us);
            } else if (strcmp(key, "TX_OFF") == 0) {
                g_ctrl_seq[4].offset_us = atoi(val);
                printf("[SET] TX_OFF offset: %d us\n", g_ctrl_seq[4].offset_us);
            } else if (strcmp(key, "RX_ON") == 0) {
                g_ctrl_seq[5].offset_us = atoi(val);
                printf("[SET] RX_ON offset: %d us\n", g_ctrl_seq[5].offset_us);
            }
        }
        token = strtok(NULL, ",");
    }
}

static void process_command(char *cmd) {
    // Trim whitespace
    while (*cmd == ' ' || *cmd == '\t') cmd++;
    char *end = cmd + strlen(cmd) - 1;
    while (end > cmd && (*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n')) *end-- = '\0';
    
    if (strlen(cmd) == 0) return;
    
    printf("[CMD] %s\n", cmd);
    
    if (strcmp(cmd, "TX_ONCE") == 0) {
        do_transmit();
    } else if (strcmp(cmd, "TX_AUTO") == 0) {
        g_tx_auto = true;
        printf("[MODE] TX Auto ON\n");
    } else if (strcmp(cmd, "TX_STOP") == 0) {
        g_tx_auto = false;
        printf("[MODE] TX Auto OFF\n");
    } else if (strcmp(cmd, "STATUS") == 0) {
        print_status();
    } else if (strncmp(cmd, "SET ", 4) == 0) {
        parse_set_command(cmd + 4);
    } else {
        printf("[ERR] Unknown command: %s\n", cmd);
    }
}

int main(void) {
    set_sys_clock_khz(OVERCLOCK_KHZ, true);
    stdio_init_all();
    
    // Czekaj na USB
    while (!stdio_usb_connected()) {
        sleep_ms(100);
    }
    sleep_ms(500);
    
    printf("\n\n");
    printf("╔═══════════════════════════════════════╗\n");
    printf("║   BPSK IONOSONDE v1.0                 ║\n");
    printf("║   Clock: %.0f MHz                     ║\n", clock_get_hz(clk_sys) / 1e6f);
    printf("╚═══════════════════════════════════════╝\n");
    printf("\nCommands: TX_ONCE, TX_AUTO, TX_STOP, STATUS\n");
    printf("          SET FREQ=x,BIT=x,AMP=x,MOD=x,...\n\n");
    
    init_control_pins();
    bpsk_gen_lut(1440, 0);
    bpsk_config(8, 8, 2.5f, 2.5f, 1);
    
    printf("[READY] Waiting for commands...\n\n");
    
    char cmd_buf[256];
    int cmd_idx = 0;
    absolute_time_t next_auto_tx = get_absolute_time();
    
    while (true) {
        // Czytaj komendy z USB
        int c = getchar_timeout_us(1000);  // 1ms timeout
        if (c != PICO_ERROR_TIMEOUT) {
            if (c == '\n' || c == '\r') {
                cmd_buf[cmd_idx] = '\0';
                if (cmd_idx > 0) {
                    process_command(cmd_buf);
                }
                cmd_idx = 0;
            } else if (cmd_idx < (int)sizeof(cmd_buf) - 1) {
                cmd_buf[cmd_idx++] = (char)c;
            }
        }
        
        // TX Auto
        if (g_tx_auto) {
            if (absolute_time_diff_us(get_absolute_time(), next_auto_tx) <= 0) {
                do_transmit();
                next_auto_tx = delayed_by_ms(get_absolute_time(), g_interval_ms);
            }
        }
    }
    
    return 0;
}
