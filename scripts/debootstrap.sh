#!/bin/sh -e

CHROOT=${CHROOT=$(pwd)/rootfs}
RELEASE=${RELEASE=stable}
HOST_NAME=${HOST_NAME=openstick}
KERNEL_APK=${KERNEL_APK=linux-postmarketos-qcom-msm8916-6.12.1-r6.apk}
WITH_ADMIN=${WITH_ADMIN=1}
. scripts/board.sh

rm -rf ${CHROOT}

# Use mmdebstrap for faster builds (god, it's so much faster)
echo "Using mmdebstrap for fast bootstrap..."
mmdebstrap --arch=arm64 \
    --include=systemd,udev,dbus,apt,wget,ca-certificates \
    --keyring=/usr/share/keyrings/debian-archive-keyring.gpg \
    ${RELEASE} ${CHROOT}

cat << EOF > ${CHROOT}/etc/apt/sources.list
deb http://deb.debian.org/debian ${RELEASE} main contrib non-free-firmware
deb http://deb.debian.org/debian-security/ ${RELEASE}-security main contrib non-free-firmware
deb http://deb.debian.org/debian ${RELEASE}-updates main contrib non-free-firmware
EOF

# Speed up apt
cat << EOF > ${CHROOT}/etc/apt/apt.conf.d/99speedup
APT::Acquire::Retries "3";
APT::Acquire::http::Timeout "10";
APT::Acquire::ftp::Timeout "10";
Acquire::Languages "none";
APT::Install-Recommends "false";
APT::Install-Suggests "false";
DPkg::Options::="--force-confdef";
DPkg::Options::="--force-confold";
EOF

mount -t proc proc ${CHROOT}/proc/
mount -t sysfs sys ${CHROOT}/sys/
mount -o bind /dev/ ${CHROOT}/dev/
mount -o bind /dev/pts/ ${CHROOT}/dev/pts/
mount -o bind /run ${CHROOT}/run/

# configs'n stuff
mkdir -p ${CHROOT}/etc/systemd/system
cp -a configs/system/* ${CHROOT}/etc/systemd/system
cp configs/nftables.conf ${CHROOT}/etc/nftables.conf
mkdir -p ${CHROOT}/etc/NetworkManager/system-connections
mkdir -p ${CHROOT}/etc/NetworkManager/conf.d
cp configs/*.nmconnection ${CHROOT}/etc/NetworkManager/system-connections
chmod 0600 ${CHROOT}/etc/NetworkManager/system-connections/*
cp configs/99-custom.conf ${CHROOT}/etc/NetworkManager/conf.d/
mkdir -p ${CHROOT}/etc/sysctl.d
cp configs/sysctl.d/*.conf ${CHROOT}/etc/sysctl.d/

# web admin page (admin/), enabled by setup.sh when present
if [ "${WITH_ADMIN}" = "1" ]; then
    install -d ${CHROOT}/opt/openstick-admin/www ${CHROOT}/etc/openstick-admin
    install -m 0644 admin/server.py admin/backend.py admin/sms.py ${CHROOT}/opt/openstick-admin/
    install -m 0644 admin/www/* ${CHROOT}/opt/openstick-admin/www/
    chmod 0700 ${CHROOT}/etc/openstick-admin
    install -m 0644 admin/openstick-admin.service ${CHROOT}/etc/systemd/system/
fi

# chroot setup
cp configs/install_dnsproxy.sh ${CHROOT}
cp scripts/setup.sh ${CHROOT}

# Copy qemu static and run setup script in chroot
cp /usr/bin/qemu-aarch64-static ${CHROOT}/usr/bin/
chroot ${CHROOT} qemu-aarch64-static /bin/sh -c "/setup.sh"

# cleanup
for a in proc sys dev/pts dev run; do
    umount ${CHROOT}/${a}
done;

rm ${CHROOT}/install_dnsproxy.sh
rm -f ${CHROOT}/setup.sh
rm -f ${CHROOT}/usr/bin/qemu-aarch64-static
echo -n > ${CHROOT}/root/.bash_history

echo ${HOST_NAME} > ${CHROOT}/etc/hostname
sed -i "/localhost/ s/$/ ${HOST_NAME}/" ${CHROOT}/etc/hosts

# setup dnsmasq
cp -a configs/dhcp.conf ${CHROOT}/etc/dnsmasq.d/dhcp.conf

# hosts entry for the LAN IP
cat <<EOF >> ${CHROOT}/etc/hosts

192.168.100.1	${HOST_NAME}
EOF

# add rc-local
cp -a configs/rc.local ${CHROOT}/etc/rc.local
chmod +x ${CHROOT}/etc/rc.local

# add MSM8916 USB gadget
cp -a configs/msm8916-usb-gadget.sh ${CHROOT}/usr/sbin/
cp configs/msm8916-usb-gadget.conf ${CHROOT}/etc/

# setup WiFi AP with hostapd
mkdir -p ${CHROOT}/etc/hostapd
cp configs/hostapd.conf ${CHROOT}/etc/hostapd/
cp configs/wifi-ap.sh ${CHROOT}/usr/sbin/
chmod +x ${CHROOT}/usr/sbin/wifi-ap.sh

cp -a scripts/msm-firmware-loader.sh ${CHROOT}/usr/sbin

# install kernel
# (r2 was removed from the mirror; check APKINDEX for the current version and
#  override with KERNEL_APK=... if this one disappears too)
wget -O - https://mirror.postmarketos.org/postmarketos/master/aarch64/${KERNEL_APK} \
    | tar xkzf - -C ${CHROOT} --exclude=.PKGINFO --exclude=.SIGN* 2>/dev/null

# The pmOS package ships an empty modules.alias and a modules.dep without
# dependencies (postmarketOS regenerates them on install). Without this no
# module is ever autoloaded: no USB gadget functions, no wcn36xx WiFi, no
# nf_tables. Regenerate with the target's kmod, which understands .ko.zst.
cp /usr/bin/qemu-aarch64-static ${CHROOT}/usr/bin/
for kver in $(ls ${CHROOT}/usr/lib/modules); do
    chroot ${CHROOT} qemu-aarch64-static /sbin/depmod -a ${kver}
    test -s ${CHROOT}/usr/lib/modules/${kver}/modules.alias
done
rm -f ${CHROOT}/usr/bin/qemu-aarch64-static

mkdir -p ${CHROOT}/boot/extlinux
sed "s/msm8916-yiming-uz801v3/${BOARD_DTB}/" configs/extlinux.conf > ${CHROOT}/boot/extlinux/extlinux.conf

# copy custom dtb's
cp dtbs/* ${CHROOT}/boot/dtbs/qcom/

# create missing directory
mkdir -p ${CHROOT}/lib/firmware/msm-firmware-loader

# update fstab
echo "PARTUUID=80780b1d-0fe1-27d3-23e4-9244e62f8c46\t/boot\text2\tdefaults\t0 2" > ${CHROOT}/etc/fstab

# backup rootfs
tar cpzf rootfs.tgz --exclude="usr/bin/qemu-aarch64-static" -C rootfs .
