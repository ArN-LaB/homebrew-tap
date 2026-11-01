# ArN-LaB Homebrew Tap

Homebrew tap for macOS apps by [ArN-LaB](https://github.com/ArN-LaB).

For **personal source builds**, see the [shared macOS local installer](developer-tools/macos-local-installer/README.md).
This developer-only component is versioned separately from casks. It is not included
in public apps, does not disable Gatekeeper and does not alter the cask below.

## Install

```bash
brew tap ArN-LaB/tap
```

## Apps

### Zenith

> Menu bar app to rank Mullvad VPN servers by latency and speed.

```bash
brew install --cask zenith
```

Requires [Mullvad VPN](https://mullvad.net) installed and logged in.

Source: [github.com/ArN-LaB/Zenith](https://github.com/ArN-LaB/Zenith)

---

## Update a cask

```bash
brew upgrade --cask zenith
```
