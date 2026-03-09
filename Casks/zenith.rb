cask "zenith" do
  version "1.0.0"
  sha256 "f4a2cf4bf80f50449637c6f43359a305829d0c109ab88be0ed0238331cae3a55"

  url "https://github.com/ArN-Ld/Zenith/releases/download/v#{version}/Zenith-#{version}.zip"
  name "Zenith"
  desc "Menu bar app to rank Mullvad VPN servers by latency and speed"
  homepage "https://github.com/ArN-Ld/Zenith"

  # Requires Mullvad VPN to be installed and logged in.
  # Bundled Python dependencies: speedtest-cli, geopy, colorama.
  # mtr optional — ping fallback activates automatically if missing.

  app "Zenith.app"

  zap trash: [
    "~/Library/Preferences/com.arn-ld.zenith.plist",
    "~/Library/Application Support/Zenith",
    "~/Library/Caches/com.arn-ld.zenith",
  ]
end
