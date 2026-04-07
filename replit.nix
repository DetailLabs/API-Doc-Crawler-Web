{ pkgs }: {
  deps = [
    pkgs.python312
    pkgs.python312Packages.pip
    pkgs.chromium
    pkgs.nss
    pkgs.nspr
    pkgs.atk
    pkgs.cups
    pkgs.libdrm
    pkgs.gtk3
    pkgs.pango
    pkgs.cairo
    pkgs.xorg.libX11
    pkgs.xorg.libXcomposite
    pkgs.xorg.libXdamage
    pkgs.xorg.libXext
    pkgs.xorg.libXrandr
    pkgs.mesa
    pkgs.alsa-lib
    pkgs.dbus
    pkgs.at-spi2-atk
    pkgs.expat
    pkgs.glib
  ];
  env = {
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";
    CHROMIUM_PATH = "${pkgs.chromium}/bin/chromium";
  };
}
