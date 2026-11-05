# prebuilt/deck

Compiled deck code for the mods, built from this repo's own sources
(tools/hotcue8, tools/stems, tools/xflac, tools/themesw, tools/keyshift_dsp,
tools/centre, tools/phasemeter, tools/utility, tools/lighttext,
tools/touchfx, tools/fx_dsp) with the SH-4 / C674x GCC toolchains
(tools/build_sh_gcc.sh, tools/build_c6x_gcc.sh). The apps install these
bytes into the deck's memory; they contain no Pioneer firmware. Kept here
because the app builds (including the Windows CI build) have no
cross-compiler. The deck installers rebuild them when a toolchain exists.
