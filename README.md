
<p align="center">
  <img src="https://readme-typing-svg.herokuapp.com?color=00FFAA&size=22&center=true&vCenter=true&width=500&lines=Boost+Your+Performance;Reduce+Input+Lag;Cleaner+Faster+System" />
</p>

# ⚡ NEXUS — Gaming Performance Suite

> A premium Windows performance optimization tool built for gamers. Clean UI, real-time hardware monitoring, one-click system tweaks, FiveM optimizations, graphic pack installer, AI assistant, and more.

![Version](https://img.shields.io/badge/version-2.0.0-gold)
![Platform](https://img.shields.io/badge/platform-Windows-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-yellow)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

### System Optimization
- **30+ Windows tweaks** — toggle individually with one click, fully reversible
- Debloat, power plan, boot speed, Win32 priority, memory management
- Core parking, timer resolution, IRQ affinity, large pages, prefetch, pagefile
- Hardware-accelerated GPU scheduling (HAGS), MPO disable, DPC latency fix
- Spectre/Meltdown mitigation toggle, process priority boost
- Disable telemetry, Superfetch, Game Bar, Win11 widgets, notifications, animations

### Network Tweaks
- TCP/IP stack optimization
- Nagle algorithm disable (lower latency)
- DNS optimization (Cloudflare / Google)
- Network adapter interrupt moderation
- Receive buffer tuning, QoS, throttle fix
- Built-in **ping test** tool

### FiveM Specific
- FiveM cache cleaner
- FiveM path auto-detection
- FiveM-optimized tweak presets
- **Graphic Pack installer** — download and install visual enhancement packs with preview video

### Hardware Monitor
- Real-time CPU, RAM, GPU usage
- Full system info (CPU model, RAM size, GPU, OS version, disk)
- Usage graphs updated live

### Cleaner
- Temp files cleaner
- Windows cache cleaner
- Recent files cleaner
- Log files cleaner
- FiveM cache cleaner

### Game Detection & Optimizer
- Auto-detects installed games
- Apply performance presets per game (Balanced / Performance / Ultra Performance)

### Startup Manager
- View and toggle Windows startup items
- Disable unnecessary startup programs to improve boot time

### Benchmark
- Built-in system benchmark tool
- Auto profile detection based on hardware

### AI Assistant
- Integrated AI chat powered by **Groq**
- Ask for optimization tips, troubleshooting, tweak explanations

### Discord Integration
- **Rich Presence (RPC)** — shows NEXUS activity in Discord status
- Webhook logging support

### License System
- HWID-bound license keys
- Offline validation — no server dependency
- Keys tied to machine hardware for security

---

## Requirements

```
Python 3.10+
pip install pywebview psutil requests rarfile GPUtil
```

---

## Installation

```bash
git clone https://github.com/yourusername/nexus-suite
cd nexus-suite
pip install pywebview psutil requests rarfile GPUtil
python main.py
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.10+ |
| UI | HTML / CSS / JavaScript |
| Window | pywebview (WebView2) |
| Hardware | psutil, GPUtil |
| AI | Groq API |
| Discord | pypresence |

---

## Screenshots

> Coming soon

---

## License

This project is licensed under the MIT License.
