#!/usr/bin/env python3
"""
BPSK Ionosonde Controller - Tkinter GUI
USB Serial control for ionosonde transmitter

Hardware setup:
- Raspberry Pi Pico @ 250 MHz
- 8-bit R-2R DAC on GP8-GP15
- 1.5W PA module controlled via GP4
- T/R antenna switch on GP3
- RX preamp enable on GP2
- TODO: RX receiver with echo detection

The system transmits BPSK chirps and waits for ionospheric echoes.
Currently only TX is implemented. RX chain is TODO.
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import serial
import serial.tools.list_ports
import threading
import time
import json
import os

class IonosondeGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("BPSK Ionosonde Controller v1.0")
        self.root.geometry("900x800")
        self.root.minsize(850, 700)
        
        self.serial_port = None
        self.serial_thread = None
        self.running = False
        self.tx_count = 0
        
        # Domyślne parametry
        self.params = {
            'frequency': 7.022,
            'bit_us': 40.0,
            'amplitude': 85,
            'modulation': 'BARKER13',
            'chip_count': 2048,
            'chip_s2s_us': 4975,
            'frame_interval_ms': 20000,
        }
        
        self.create_widgets()
        self.scan_ports()
        
    def create_widgets(self):
        # === Main frame ===
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # === System info banner ===
        info_frame = ttk.LabelFrame(main_frame, text="System Info", padding=5)
        info_frame.pack(fill=tk.X, pady=(0, 10))
        info_text = ("BPSK Ionosonde TX: R-2R DAC → 1.5W PA → T/R Switch → Antenna\n"
                    "GPIO: GP2=RX Enable, GP3=T/R Switch, GP4=PA Enable, GP8-15=DAC\n"
                    "Status: TX implemented ✓ | RX receiver: TODO")
        ttk.Label(info_frame, text=info_text, font=('Consolas', 9), foreground='#333').pack(anchor=tk.W)
        
        # === Serial connection ===
        conn_frame = ttk.LabelFrame(main_frame, text="Serial Connection", padding=10)
        conn_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(conn_frame, text="Port:").pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(conn_frame, width=20, state="readonly")
        self.port_combo.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(conn_frame, text="🔄 Refresh", command=self.scan_ports).pack(side=tk.LEFT, padx=2)
        
        self.connect_btn = ttk.Button(conn_frame, text="🔌 Connect", command=self.toggle_connection)
        self.connect_btn.pack(side=tk.LEFT, padx=5)
        
        self.status_label = ttk.Label(conn_frame, text="● Disconnected", foreground="red")
        self.status_label.pack(side=tk.RIGHT)
        
        # === TX Parameters ===
        params_frame = ttk.LabelFrame(main_frame, text="TX Parameters", padding=10)
        params_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Grid dla parametrów
        params_grid = ttk.Frame(params_frame)
        params_grid.pack(fill=tk.X)
        
        # Frequency
        ttk.Label(params_grid, text="Carrier Freq [MHz]:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.freq_var = tk.DoubleVar(value=self.params['frequency'])
        self.freq_spin = ttk.Spinbox(params_grid, from_=1.0, to=30.0, increment=0.001, 
                                      textvariable=self.freq_var, width=12)
        self.freq_spin.grid(row=0, column=1, padx=5, pady=2)
        
        # Bit duration
        ttk.Label(params_grid, text="Bit Duration [μs]:").grid(row=0, column=2, sticky=tk.W, pady=2, padx=(20,0))
        self.bit_var = tk.DoubleVar(value=self.params['bit_us'])
        self.bit_spin = ttk.Spinbox(params_grid, from_=10, to=500, increment=5, 
                                     textvariable=self.bit_var, width=12)
        self.bit_spin.grid(row=0, column=3, padx=5, pady=2)
        
        # Amplitude
        ttk.Label(params_grid, text="Amplitude [%]:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.amp_var = tk.IntVar(value=self.params['amplitude'])
        self.amp_scale = ttk.Scale(params_grid, from_=0, to=100, variable=self.amp_var, 
                                    orient=tk.HORIZONTAL, length=150)
        self.amp_scale.grid(row=1, column=1, padx=5, pady=2)
        self.amp_label = ttk.Label(params_grid, text=f"{self.params['amplitude']}%")
        self.amp_label.grid(row=1, column=1, sticky=tk.E)
        self.amp_var.trace('w', lambda *args: self.amp_label.config(text=f"{self.amp_var.get()}%"))
        
        # Modulation
        ttk.Label(params_grid, text="Modulation:").grid(row=1, column=2, sticky=tk.W, pady=2, padx=(20,0))
        self.mod_var = tk.StringVar(value=self.params['modulation'])
        self.mod_combo = ttk.Combobox(params_grid, textvariable=self.mod_var, width=12, state="readonly",
                                       values=['CARRIER', 'BARKER2', 'BARKER3', 'BARKER4', 'BARKER5', 
                                               'BARKER7', 'BARKER11', 'BARKER13'])
        self.mod_combo.grid(row=1, column=3, padx=5, pady=2)
        
        # Chip count
        ttk.Label(params_grid, text="Chip Count:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.chip_var = tk.IntVar(value=self.params['chip_count'])
        self.chip_spin = ttk.Spinbox(params_grid, from_=1, to=10000, increment=100, 
                                      textvariable=self.chip_var, width=12)
        self.chip_spin.grid(row=2, column=1, padx=5, pady=2)
        
        # Chip S2S
        ttk.Label(params_grid, text="Chip S2S [μs]:").grid(row=2, column=2, sticky=tk.W, pady=2, padx=(20,0))
        self.s2s_var = tk.IntVar(value=self.params['chip_s2s_us'])
        self.s2s_spin = ttk.Spinbox(params_grid, from_=100, to=100000, increment=100, 
                                     textvariable=self.s2s_var, width=12)
        self.s2s_spin.grid(row=2, column=3, padx=5, pady=2)
        
        # Frame interval
        ttk.Label(params_grid, text="Frame Interval [ms]:").grid(row=3, column=0, sticky=tk.W, pady=2)
        self.interval_var = tk.IntVar(value=self.params['frame_interval_ms'])
        self.interval_spin = ttk.Spinbox(params_grid, from_=1000, to=60000, increment=1000, 
                                          textvariable=self.interval_var, width=12)
        self.interval_spin.grid(row=3, column=1, padx=5, pady=2)
        
        # === GPIO Control Sequence ===
        gpio_frame = ttk.LabelFrame(main_frame, text="GPIO Control Sequence (T/R Switch + 1.5W PA timing)", padding=10)
        gpio_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Description
        desc_text = ("Each TX chip has a GPIO sequence: RX_EN (GP2), T/R Switch (GP3), PA 1.5W (GP4).\n"
                    "Offsets in μs: negative = BEFORE chip start, positive = AFTER chip end.\n"
                    "Example: RX_OFF=-200 means disable RX preamp 200μs before TX starts.")
        ttk.Label(gpio_frame, text=desc_text, font=('Helvetica', 8), foreground='gray').pack(anchor=tk.W)
        
        gpio_grid = ttk.Frame(gpio_frame)
        gpio_grid.pack(fill=tk.X, pady=(5, 0))
        
        # Headers
        ttk.Label(gpio_grid, text="Event", font=('Helvetica', 9, 'bold')).grid(row=0, column=0, sticky=tk.W, padx=5)
        ttk.Label(gpio_grid, text="Offset [μs]", font=('Helvetica', 9, 'bold')).grid(row=0, column=1, padx=5)
        ttk.Label(gpio_grid, text="Description", font=('Helvetica', 9, 'bold')).grid(row=0, column=2, sticky=tk.W, padx=10)
        
        # Default sequence values
        self.gpio_events = {
            'rx_off': {'var': tk.IntVar(value=-200), 'desc': 'Disable RX preamp to protect from TX power'},
            'tx_on': {'var': tk.IntVar(value=-150), 'desc': 'Switch antenna relay to TX path'},
            'pa_on': {'var': tk.IntVar(value=-100), 'desc': 'Enable 1.5W PA module (needs settling time)'},
            'pa_off': {'var': tk.IntVar(value=10), 'desc': 'Disable PA after chip transmission'},
            'tx_off': {'var': tk.IntVar(value=40), 'desc': 'Switch antenna relay back to RX path'},
            'rx_on': {'var': tk.IntVar(value=60), 'desc': 'Enable RX preamp for echo reception'},
        }
        
        event_labels = [
            ('rx_off', '1. RX OFF (GP2=0)', 'PRE'),
            ('tx_on', '2. T/R→TX (GP3=1)', 'PRE'),
            ('pa_on', '3. PA ON (GP4=1)', 'PRE'),
            ('pa_off', '4. PA OFF (GP4=0)', 'POST'),
            ('tx_off', '5. T/R→RX (GP3=0)', 'POST'),
            ('rx_on', '6. RX ON (GP2=1)', 'POST'),
        ]
        
        for i, (key, label, phase) in enumerate(event_labels, start=1):
            ev = self.gpio_events[key]
            color = '#006600' if phase == 'PRE' else '#660000'
            ttk.Label(gpio_grid, text=label, foreground=color).grid(row=i, column=0, sticky=tk.W, padx=5, pady=1)
            spin = ttk.Spinbox(gpio_grid, from_=-1000, to=1000, increment=10, 
                               textvariable=ev['var'], width=8)
            spin.grid(row=i, column=1, padx=5, pady=1)
            ttk.Label(gpio_grid, text=ev['desc'], font=('Helvetica', 8), foreground='gray').grid(row=i, column=2, sticky=tk.W, padx=10, pady=1)

        # === TX Control ===
        ctrl_frame = ttk.LabelFrame(main_frame, text="TX Control", padding=10)
        ctrl_frame.pack(fill=tk.X, pady=(0, 10))
        
        btn_frame = ttk.Frame(ctrl_frame)
        btn_frame.pack()
        
        self.tx_once_btn = ttk.Button(btn_frame, text="📡 TX Once", command=self.tx_once, width=15)
        self.tx_once_btn.pack(side=tk.LEFT, padx=5)
        
        self.tx_auto_btn = ttk.Button(btn_frame, text="🔄 TX Auto", command=self.tx_auto, width=15)
        self.tx_auto_btn.pack(side=tk.LEFT, padx=5)
        
        self.tx_stop_btn = ttk.Button(btn_frame, text="⏹ Stop", command=self.tx_stop, width=15, state=tk.DISABLED)
        self.tx_stop_btn.pack(side=tk.LEFT, padx=5)
        
        self.send_params_btn = ttk.Button(btn_frame, text="📤 Send Params", command=self.send_params, width=15)
        self.send_params_btn.pack(side=tk.LEFT, padx=5)
        
        # TX counter
        self.tx_count_label = ttk.Label(ctrl_frame, text="TX Count: 0", font=('Helvetica', 12, 'bold'))
        self.tx_count_label.pack(pady=5)
        
        # === Serial Log ===
        log_frame = ttk.LabelFrame(main_frame, text="Serial Log", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=80, 
                                                   font=('Consolas', 9), state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
        # Log buttons
        log_btn_frame = ttk.Frame(log_frame)
        log_btn_frame.pack(fill=tk.X, pady=(5, 0))
        ttk.Button(log_btn_frame, text="🗑 Clear Log", command=self.clear_log).pack(side=tk.LEFT)
        ttk.Button(log_btn_frame, text="💾 Save Log", command=self.save_log).pack(side=tk.LEFT, padx=5)
        
    def scan_ports(self):
        """Scan available serial ports"""
        ports = [p.device for p in serial.tools.list_ports.comports()]
        # Prefer ttyACM
        ports.sort(key=lambda x: (0 if 'ttyACM' in x else 1, x))
        self.port_combo['values'] = ports
        if ports:
            self.port_combo.current(0)
        self.log(f"Found ports: {ports if ports else 'none'}")
        
    def toggle_connection(self):
        """Toggle serial connection"""
        if self.serial_port and self.serial_port.is_open:
            self.disconnect()
        else:
            self.connect()
            
    def connect(self):
        """Connect to serial port"""
        port = self.port_combo.get()
        if not port:
            messagebox.showerror("Error", "Select a serial port!")
            return
            
        try:
            self.serial_port = serial.Serial(port, 115200, timeout=0.1)
            self.running = True
            self.serial_thread = threading.Thread(target=self.read_serial, daemon=True)
            self.serial_thread.start()
            
            self.status_label.config(text=f"● Connected: {port}", foreground="green")
            self.connect_btn.config(text="🔌 Disconnect")
            self.log(f"Connected to {port}")
        except Exception as e:
            messagebox.showerror("Connection Error", str(e))
            self.log(f"ERROR: {e}")
            
    def disconnect(self):
        """Disconnect serial"""
        self.running = False
        if self.serial_port:
            self.serial_port.close()
            self.serial_port = None
        self.status_label.config(text="● Disconnected", foreground="red")
        self.connect_btn.config(text="🔌 Connect")
        self.log("Disconnected")
        
    def read_serial(self):
        """Serial read thread"""
        while self.running and self.serial_port:
            try:
                if self.serial_port.in_waiting:
                    line = self.serial_port.readline().decode('utf-8', errors='ignore').strip()
                    if line:
                        self.root.after(0, lambda l=line: self.process_line(l))
            except Exception as e:
                if self.running:
                    self.root.after(0, lambda: self.log(f"Read error: {e}"))
                break
            time.sleep(0.01)
            
    def process_line(self, line):
        """Process serial line"""
        self.log(line)
        # Count TX
        if "[TX-" in line and "started" in line:
            self.tx_count += 1
            self.tx_count_label.config(text=f"TX Count: {self.tx_count}")
            
    def log(self, msg):
        """Add log entry"""
        self.log_text.config(state=tk.NORMAL)
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {msg}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
        
    def clear_log(self):
        """Clear log"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)
        
    def save_log(self):
        """Save log to file"""
        filename = f"ionosonde_log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        self.log_text.config(state=tk.NORMAL)
        content = self.log_text.get(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)
        with open(filename, 'w') as f:
            f.write(content)
        self.log(f"Log saved: {filename}")
        
    def send_command(self, cmd):
        """Send command via serial"""
        if not self.serial_port or not self.serial_port.is_open:
            messagebox.showwarning("Warning", "Connect to serial port first!")
            return False
        try:
            self.serial_port.write(f"{cmd}\n".encode())
            self.log(f">>> {cmd}")
            return True
        except Exception as e:
            self.log(f"Send error: {e}")
            return False
            
    def get_params_string(self):
        """Build params string"""
        return (f"FREQ={self.freq_var.get():.6f},"
                f"BIT={self.bit_var.get():.1f},"
                f"AMP={self.amp_var.get()/100:.2f},"
                f"MOD={self.mod_var.get()},"
                f"CHIPS={self.chip_var.get()},"
                f"S2S={self.s2s_var.get()},"
                f"INTERVAL={self.interval_var.get()},"
                f"RX_OFF={self.gpio_events['rx_off']['var'].get()},"
                f"TX_ON={self.gpio_events['tx_on']['var'].get()},"
                f"PA_ON={self.gpio_events['pa_on']['var'].get()},"
                f"PA_OFF={self.gpio_events['pa_off']['var'].get()},"
                f"TX_OFF={self.gpio_events['tx_off']['var'].get()},"
                f"RX_ON={self.gpio_events['rx_on']['var'].get()}")
                
    def send_params(self):
        """Send params to Pico"""
        params = self.get_params_string()
        self.send_command(f"SET {params}")
        
    def tx_once(self):
        """Transmit single frame"""
        self.send_command("TX_ONCE")
        
    def tx_auto(self):
        """Enable auto transmission"""
        if self.send_command("TX_AUTO"):
            self.tx_auto_btn.config(state=tk.DISABLED)
            self.tx_stop_btn.config(state=tk.NORMAL)
            
    def tx_stop(self):
        """Stop transmission"""
        if self.send_command("TX_STOP"):
            self.tx_auto_btn.config(state=tk.NORMAL)
            self.tx_stop_btn.config(state=tk.DISABLED)
            
    def on_close(self):
        """Close application"""
        self.running = False
        if self.serial_port:
            self.serial_port.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = IonosondeGUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
