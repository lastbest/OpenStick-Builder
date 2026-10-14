# Board selection, sourced by build_hyp_aboot.sh and debootstrap.sh.
#   BOARD=uz801         YiMing UZ801 v3 (default, upstream behaviour)
#   BOARD=jz01-45-v33   JZ01-45-V33 "juzhen/JZ" UFI dongles (e.g. UF902)
#   BOARD=ufi001c       UFI-001B/C
# LK2ND_COMPATIBLE / BOARD_DTB can still be overridden individually.
BOARD=${BOARD:-uz801}
case "${BOARD}" in
    uz801)       _compat="yiming,uz801-v3";       _dtb="msm8916-yiming-uz801v3" ;;
    jz01-45-v33) _compat="openstick,jz01-45-v33"; _dtb="msm8916-jz01-45-v33" ;;
    ufi001c)     _compat="thwc,ufi001c";          _dtb="msm8916-thwc-ufi001c" ;;
    *) echo "Unknown BOARD=${BOARD} (uz801 | jz01-45-v33 | ufi001c)" >&2; exit 1 ;;
esac
LK2ND_COMPATIBLE=${LK2ND_COMPATIBLE:-${_compat}}
BOARD_DTB=${BOARD_DTB:-${_dtb}}
