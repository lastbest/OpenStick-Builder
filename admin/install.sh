#!/bin/sh -e
# Install / update the admin page on an already flashed device (run as root).
# Images built with WITH_ADMIN=1 (default) already contain it.
cd "$(dirname "$0")"

install -d /opt/openstick-admin/www /etc/openstick-admin
install -m 0644 server.py backend.py /opt/openstick-admin/
install -m 0644 www/* /opt/openstick-admin/www/
chmod 0700 /etc/openstick-admin
install -m 0644 openstick-admin.service /etc/systemd/system/

systemctl daemon-reload
systemctl enable openstick-admin
systemctl restart openstick-admin
echo "Open http://192.168.100.1/ (first visit asks for an admin password)"
