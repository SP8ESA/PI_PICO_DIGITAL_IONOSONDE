# BPSK Digital Ionosonde# BPSK Ionosonde# BPSK Ionosonde# BPSK Ionosonde - Projekt Jonosondy



A low-cost digital ionosonde based on Raspberry Pi Pico with BPSK modulation for ionospheric sounding. Successfully detects ionospheric echoes with O/X mode splitting.



![GUI Controller](img/gui.png)**Raspberry Pi Pico-based digital ionosonde with BPSK modulation for ionospheric sounding.**



## Overview



This project implements a complete ionosonde transmitter using a Raspberry Pi Pico microcontroller. The system generates BPSK-modulated pulses at HF frequencies and controls the RF chain timing for transmit/receive switching.Low-cost ionosonde transmitter capable of detecting ionospheric echoes with ordinary/extraordinary ray splitting (O/X mode separation).Raspberry Pi Pico-based ionosonde transmitter with BPSK modulation.## Opis projektu



**Key achievement:** Real ionospheric echoes have been successfully received, showing ordinary and extraordinary wave splitting (O/X modes).



## Hardware![GUI Controller](img/gui.png)



### Block Diagram



```## Hardware Setup## Hardware SetupProjekt zoptymalizowanej jonosondy opartej na modulacji BPSK (Binary Phase Shift Keying) dla Raspberry Pi Pico. System transmituje sondaż ionosfery na częstotliwości 7.022 MHz z sekwencją modulacji Barker-13.

┌──────────────────────────────────────────────────────────────────────┐

│  Raspberry Pi Pico (RP2040 @ 250 MHz)                                │

│                                                                      │

│  ┌──────────┐    ┌─────────┐    ┌────────────┐    ┌──────────┐      │```

│  │ GP8-GP15 │───→│  8-bit  │───→│  2-stage   │───→│   T/R    │───→ ANT

│  │  (PIO)   │    │ R-2R DAC│    │    PA      │    │  Switch  │      │┌─────────────────────────────────────────────────────────────────────────┐

│  └──────────┘    └─────────┘    └────────────┘    └──────────┘      │

│                                       │                │             ││  Raspberry Pi Pico (250MHz overclock)                                   │```## Cechy

│                          GP4 (PA_EN) ─┘                │             │

│                          GP3 (TR_SW) ──────────────────┘             ││  ┌──────────┐    ┌─────────┐    ┌────────────────┐    ┌─────────────┐  │

│                          GP2 (RX_EN) ───────────────────────→ RX    │

└──────────────────────────────────────────────────────────────────────┘│  │ GP8-GP15 │───→│ 8-bit   │───→│ 2-stage PA     │───→│ T/R Switch  │──→ Antenna┌─────────────────────────────────────────────────────────────┐

```

│  │ (R-2R)   │    │ R-2R DAC│    │ SBB5089Z +     │    │ PE42553B    │  │

### Components

│  └──────────┘    └─────────┘    │ RD01MUS2B 1.5W │    └─────────────┘  ││  Pico (250MHz)                                              │✅ **Modulacja BPSK** - binarna modulacja fazy z sekwencjami Barkera  

| Component | Part Number | Function |

|-----------|-------------|----------|│                                 └────────────────┘          │          │

| MCU | Raspberry Pi Pico | Signal generation, timing control |

| DAC | 8-bit R-2R Ladder | Digital to analog conversion (GP8-GP15) |│       GP4 (PA_EN) ─────────────────────┘                    │          ││  ┌──────────┐    ┌─────────┐    ┌──────────┐    ┌────────┐ │✅ **R-2R Ladder DAC** - 8-bitowy przetwornik cyfrowo-analogowy  

| Driver | SBB5089Z | MMIC preamplifier stage |

| Final PA | RD01MUS2B | 1.5W RF power MOSFET |│       GP3 (T/R SW) ─────────────────────────────────────────┘          │

| T/R Switch | PE42553B | SPDT antenna switch |

│       GP2 (RX_EN) ──────────────────────────────────────────→ RX (TODO)││  │ GP8-GP15 │───→│ 8-bit   │───→│  1.5W PA │───→│  T/R   │──→ Antenna✅ **PIO + DMA** - precyzyjne sterowanie wyjściem z wykorzystaniem PIO i DMA  

### GPIO Pinout

└─────────────────────────────────────────────────────────────────────────┘

| Pin | Function | Description |

|-----|----------|-------------|```│  │ (R-2R)   │    │ R-2R DAC│    │  Module  │    │ Switch │ │✅ **Harmonogram TX** - automatyczne ramki co 20 sekund  

| GP2 | RX_EN | RX preamplifier enable (HIGH = ON) |

| GP3 | TR_SW | T/R switch control (HIGH = TX, LOW = RX) |

| GP4 | PA_EN | Power amplifier enable (HIGH = ON) |

| GP8-GP15 | DAC | 8-bit R-2R DAC output |### Components│  └──────────┘    └─────────┘    └──────────┘    └────────┘ │✅ **Sterowanie GPIO** - sekwencyjne włączanie/wyłączanie wzmacniacza, przełącznika T/R i odbiornika  



## Signal Generation



The Pico generates BPSK-modulated signals using PIO and DMA for precise, jitter-free timing:| Component | Part Number | Description |│                                      │              │       │✅ **USB Debug** - raportowanie statusu przez USB  



![Generated Signal](img/signal.png)|-----------|-------------|-------------|



*Top: BPSK modulated carrier. Bottom: GPIO control signals for PA and T/R switch.*| MCU | Raspberry Pi Pico | RP2040 @ 250MHz |│       GP4 (PA_EN) ──────────────────┘              │       │✅ **Soft Phase Transitions** - wygładzone przejścia fazy między bitami  



## Results| DAC | R-2R Ladder | 8-bit on GP8-GP15 |



Ionospheric echoes showing O-mode and X-mode separation:| PA Stage 1 | SBB5089Z | MMIC driver amplifier |│       GP3 (T/R SW) ────────────────────────────────┘       │✅ **Fade-in/Fade-out** - amplitudowe wygładzanie na początkach/końcach



![Ionospheric Echoes](img/echo.png)| PA Stage 2 | RD01MUS2B | 1.5W RF MOSFET final |



*Reception via SDRplay RSPduo. Native Pico RX implementation is planned.*| T/R Switch | PE42553B | SPDT RF switch |│       GP2 (RX_EN) ─────────────────────────────────→ RX Preamp (TODO)



## Features



- **Modulation:** BPSK with selectable Barker codes (2, 3, 4, 5, 7, 11, 13)## Generated Signal└─────────────────────────────────────────────────────────────┘## Parametry TX

- **Frequency:** Configurable, default 7.022 MHz

- **Output:** ~1.5W via 2-stage amplifier

- **Timing:** PIO + DMA for sample-accurate signal generation

- **Control:** Python GUI with serial interfacePico generates BPSK modulated signal with GPIO control for PA and T/R switching:```

- **GPIO Sequencing:** Precise T/R switch and PA timing control



## TX Timing Sequence

![Signal and GPIO timing](img/signal.png)| Parameter | Value | Jednostka |

Each chip transmission follows this sequence:



```

Time:   -200us   -150us   -100us    0      [TX]    +10us   +40us   +60us## Ionospheric Echoes## Features|-----------|-------|-----------|

          │        │        │       │                │        │       │

          ▼        ▼        ▼       ▼                ▼        ▼       ▼

        RX OFF   TR→TX    PA ON   START           PA OFF   TR→RX   RX ON

```Recorded ionospheric reflections showing O-mode / X-mode splitting (ordinary and extraordinary rays):| Carrier Frequency | 7.022 | MHz |



## Parameters



| Parameter | Default | Range | Description |![Ionospheric echo with O/X splitting](img/echo.png)- **TX**: BPSK modulation with Barker codes (2-13)| Modulation | BPSK Barker-13 | - |

|-----------|---------|-------|-------------|

| Frequency | 7.022 MHz | 1-30 MHz | Carrier frequency |

| Bit Duration | 40 µs | 10-1000 µs | Duration of each BPSK bit |

| Amplitude | 85% | 0-100% | DAC output level |*Currently received using RSPduo SDR. Native Pico RX is planned.*- **DAC**: 8-bit R-2R ladder on GP8-GP15| Bit Duration | 40 | μs |

| Modulation | BARKER13 | BARKER2-13 | Barker code selection |

| Chip Count | 2048 | 1-65535 | Number of chips per frame |

| Chip Interval | 4975 µs | 100-100000 µs | Time between chip starts |

| Frame Interval | 20000 ms | 1000-60000 ms | Auto-TX repetition rate |## Features- **PA**: 1.5W power amplifier with GPIO control| Chip Count | 2048 | - |



## Getting Started



### Build Firmware- **TX**: BPSK modulation with Barker codes (2-13)- **T/R Switch**: Antenna relay for TX/RX switching| Chip S2S | 4975 | μs |



```bash- **DAC**: 8-bit R-2R ladder on GP8-GP15

cd build

cmake ..- **PA**: 2-stage amplifier (SBB5089Z + RD01MUS2B) ~1.5W- **GUI**: Python Tkinter control panel| Frame Duration | ~10 | s |

make

```- **T/R Switch**: PE42553B SPDT for antenna switching



### Flash Pico- **GUI**: Python Tkinter control panel| Frame Interval | 20 | s |



```bash

picotool load pico_digisonde.elf -fx

```## Project Status## Status| TX Amplitude | 0.85 | (0..1) |



### Run GUI



```bash- ✅ TX transmitter - implemented| System Clock | 250 | MHz |

pip install pyserial

python3 ionosonde_gui.py- ✅ GPIO sequencing - implemented  

```

- ✅ GUI controller - implemented- ✅ TX transmitter - implemented

### Operation

- ✅ Ionospheric echoes - confirmed!

1. Connect to serial port (typically `/dev/ttyACM0` on Linux)

2. Adjust transmission parameters as needed- ❌ RX receiver on Pico - TODO (currently using RSPduo)- ✅ GPIO sequencing - implemented  ## Piny GPIO

3. Click **Send Params** to upload settings

4. Click **TX Once** for single transmission or **TX Auto** for continuous operation



## Serial Protocol## GPIO Pinout- ✅ GUI controller - implemented



| Command | Description |

|---------|-------------|

| `TX_ONCE` | Transmit single frame || GPIO | Function | Description |- ❌ RX receiver - TODO| GPIO | Funkcja | Opis |

| `TX_AUTO` | Start automatic transmission |

| `TX_STOP` | Stop transmission ||------|----------|-------------|

| `STATUS` | Query current parameters |

| `SET FREQ=7.022,BIT_US=40,...` | Set parameters || GP2 | RX_EN | RX preamp enable (1=ON) ||------|---------|------|



## Project Status| GP3 | T/R_SW | PE42553B control (1=TX, 0=RX) |



- ✅ BPSK transmitter with PIO/DMA| GP4 | PA_EN | PA enable (1=ON) |## GPIO Pinout| GP8-GP15 | R-2R DAC Output | 8-bitowy wyjście nośnej BPSK |

- ✅ GPIO sequencing for T/R and PA control

- ✅ Python GUI controller| GP8-15 | DAC | 8-bit R-2R output |

- ✅ Ionospheric echo reception confirmed

- ⬜ Native RX receiver (planned)| GP2 | RX Enable | 1=RX ON, 0=RX OFF |

- ⬜ Real-time ionogram display (planned)

## TX Sequence (per chip)

## Files

| GPIO | Function | Description || GP3 | T/R Switch | 1=TX, 0=RX |

```

├── main.c              # Main firmware```

├── bpsk_tx.c           # BPSK transmitter library

├── bpsk_tx.h           # Header fileTime:  -200μs  -150μs  -100μs    0    [CHIP TX]   +10μs  +40μs  +60μs|------|----------|-------------|| GP4 | PA Enable | 1=PA ON, 0=PA OFF |

├── ionosonde_gui.py    # Python control GUI

├── CMakeLists.txt      # Build configuration        │        │        │      │                  │       │       │

└── img/                # Documentation images

```        ▼        ▼        ▼      ▼                  ▼       ▼       ▼| GP2 | RX_EN | RX preamp enable (1=ON) |



## Requirements     RX OFF   T/R→TX   PA ON   START             PA OFF  T/R→RX  RX ON



- Raspberry Pi Pico```| GP3 | T/R_SW | Antenna switch (1=TX, 0=RX) |## Architektura kodu

- Pico SDK 2.x

- Python 3.x with pyserial



## License## Quick Start| GP4 | PA_EN | 1.5W PA enable (1=ON) |



MIT



## Author### 1. Flash Pico| GP8-15 | DAC | 8-bit R-2R output |### bpsk_tx.h / bpsk_tx.c



SP8ESA```bash


cd buildBiblioteka BPSK TX zawierająca:

cmake .. && make

picotool load pico_digisonde.elf -fx## TX Sequence (per chip)- **Generowanie LUT sinusa** - precompute lookup table dla efektywności

```

- **Konfiguracja PIO** - program 1-instrukcyjny dla wyjścia R-2R

### 2. Run GUI

```bash```- **Budowanie bufora chipa** - synteza sygnału BPSK z blending przejść fazy

pip install pyserial

python3 ionosonde_gui.pyTime:  -200μs  -150μs  -100μs    0    [CHIP TX]   +10μs  +40μs  +60μs- **DMA streaming** - efektywne wysyłanie danych do PIO

```

        │        │        │      │                  │       │       │- **Sterowanie GPIO** - precyzyjne zdarzenia względem chipów

### 3. Connect & Transmit

1. Select serial port (usually `/dev/ttyACM0`)        ▼        ▼        ▼      ▼                  ▼       ▼       ▼

2. Click "Connect"

3. Adjust parameters     RX OFF   T/R→TX   PA ON   START             PA OFF  T/R→RX  RX ON### main.c

4. Click "Send Params"

5. Click "TX Once" or "TX Auto"```Program główny:



## Parameters- Inicjalizacja zegara (250 MHz overclock)



| Parameter | Default | Description |## Quick Start- Setup GPIO sterowania

|-----------|---------|-------------|

| Frequency | 7.022 MHz | Carrier frequency |- Harmonogram transmisji (co 20s)

| Bit Duration | 40 μs | BPSK bit length |

| Amplitude | 85% | DAC output level |### 1. Flash Pico- Loop TX z raportowaniem statusu

| Modulation | BARKER13 | Barker code type |

| Chip Count | 2048 | Chips per frame |```bash

| Chip S2S | 4975 μs | Start-to-start spacing |

| Frame Interval | 20000 ms | Auto TX interval |cd build## Sekwencja TX



## Serial Commandscmake .. && make



```picotool load pico_digisonde.elf -fx```

TX_ONCE              - Transmit single frame

TX_AUTO              - Start auto transmission```START FRAME

TX_STOP              - Stop transmission

STATUS               - Show current parameters├─ PRE: RX_OFF (-200μs) → TX_ON (-150μs) → PA_ON (-100μs)

SET FREQ=7.022,...   - Set parameters

```### 2. Run GUI├─ [2048 CHIPÓW BPSK]



## Repository Structure```bash└─ POST: PA_OFF (+10μs) → TX_OFF (+40μs) → RX_ON (+60μs)



```python3 ionosonde_gui.py

PI_PICO_DIGITAL_IONOSONDE/

├── main.c              # Pico firmware```IDLE: 20s - 10s = 10s oczekiwania

├── bpsk_tx.c/h         # BPSK TX library

├── ionosonde_gui.py    # Python GUI controller```

├── CMakeLists.txt      # Build configuration

├── img/### 3. Connect & Transmit

│   ├── gui.png         # GUI screenshot

│   ├── signal.png      # Generated signal + GPIO1. Select serial port (usually `/dev/ttyACM0`)## Kompilacja

│   └── echo.png        # Ionospheric echoes

└── ref/                # Reference files2. Click "Connect"

```

3. Adjust parameters```bash

## Requirements

4. Click "Send Params"cd /home/sp8esa/pico_digisonde

- Raspberry Pi Pico

- Python 3 + pyserial5. Click "TX Once" or "TX Auto"mkdir -p build

- Pico SDK 2.x

cd build

## TODO

## Parameterscmake ..

- [ ] Native RX receiver on Pico

- [ ] Real-time echo detectionmake

- [ ] Ionogram display

- [ ] Frequency sweep mode| Parameter | Default | Description |# lub w VS Code: Run Task "Compile Project"

- [ ] Automatic layer detection (E, F1, F2)

|-----------|---------|-------------|```

## Author

| Frequency | 7.022 MHz | Carrier frequency |

SP8ESA

| Bit Duration | 40 μs | BPSK bit length |## Flashing

## License

| Amplitude | 85% | DAC output level |

MIT

| Modulation | BARKER13 | Barker code type |```bash

| Chip Count | 2048 | Chips per frame |# Metoda 1: Picotool

| Chip S2S | 4975 μs | Start-to-start spacing |picotool load build/pico_digisonde.elf -fx

| Frame Interval | 20000 ms | Auto TX interval |

# Metoda 2: OpenOCD

## Serial Commandsopenocd -s ~/.pico-sdk/openocd/0.12.0+dev/scripts \

  -f interface/cmsis-dap.cfg \

```  -f target/rp2040.cfg \

TX_ONCE              - Transmit single frame  -c "program build/pico_digisonde.elf verify reset exit"

TX_AUTO              - Start auto transmission

TX_STOP              - Stop transmission# Metoda 3: VS Code Task "Flash"

STATUS               - Show current params```

SET FREQ=7.022,...   - Set parameters

```## Debugowanie



## FilesMonitor USB:

```bash

```minicom -D /dev/ttyACM0 -b 115200

pico_digisonde/# lub

├── main.c              # Pico firmwarescreen /dev/ttyACM0 115200

├── bpsk_tx.c/h         # BPSK TX library```

├── ionosonde_gui.py    # Python GUI

├── CMakeLists.txt      # Build configLogi zawierają:

└── ref/                # Reference files- Boot info z freq zegara

```- Status inicjalizacji LUT i PIO

- Numer ramki i timestamp TX

## Requirements- Status powodzenia transmisji



- Raspberry Pi Pico## Optymalizacje zastosowane

- Python 3 + pyserial (`pip install pyserial`)

- Pico SDK1. **Wygenerowana LUT sinusa** - zamiast obliczania sin() w locie

2. **DMA do PIO** - CPU nie jest zaangażowany w streamowanie

## TODO3. **Fixed-point arytmetyka** - Q32.32 dla precyzji bez FPU

4. **Blending przejść fazy** - cosinus window dla miękkiego przejścia

- [ ] RX receiver implementation5. **Fade Hann** - amplitudowe wygładzanie na krańcach

- [ ] Echo detection & processing6. **Memory pooling** - reuse bufora na każdy chip

- [ ] Ionogram display7. **GPIO predicts** - pre-compute eventy absoluta

- [ ] Frequency sweep mode

## Dostosowywanie

## License

### Zmiana częstotliwości

MIT```c

#define FREQ_HZ  7022000.0  // edit w main.c
```

### Zmiana sekwencji modulacji
```c
bpsk_transmit(
    "BARKER7",          // zmień Barker: BARKER2..BARKER13
    // lub własna: "101001"
    ...
)
```

### Zmiana amplitudy
```c
#define TX_AMPLITUDE  0.85f  // (0.0 .. 1.0)
```

### Zmiana harmonogramu
```c
#define FRAME_INTERVAL_MS  20000  // co ile ms (ms)
```

### Dostosowanie offsetów GPIO
Edit `CTRL_SEQ[]` w main.c, zmień offset_us dla każdego pinu.

## Uwagi bezpieczeństwa

⚠️ **Wzmacniacz PA** - czasowy schemat (PRE/POST) musi być zsynchronizowany z czasem wzmacniania PA!  
⚠️ **T/R Switch** - ensure duplexer delay alignment  
⚠️ **Przetwornik R-2R** - wymaga właściwego filtrowania wyjścia (RC lowpass)  

## Reference

- Pico SDK: https://github.com/raspberrypi/pico-sdk
- PIO Documentation: https://datasheets.raspberrypi.org/rp2040/rp2040-datasheet.pdf
- BPSK Modulation: https://en.wikipedia.org/wiki/Phase-shift_keying

## Wersja

- **v0.2** - Zoptymalizowana jonosonda BPSK
- Bazuje na ref/bpsk_tx.c i ref/bpsk_tx.h z poprawkami

---

**Autor**: Optimized for pico_digisonde  
**Data**: 22 stycznia 2026  
**Status**: ✅ Kompilacja OK, gotowe do flashing
