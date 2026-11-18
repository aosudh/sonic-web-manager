#!/bin/sh
# HPE R1N26A / Celestica D3030 Questone_2, AMI BMC firmware 0.32.
# Only this verified device's private BMC-to-SONiC USB link is managed.
# BusyBox 1.21-compatible shell; no Python/IPMI/BMC reset/firmware writes.
# Deployed as /conf/sonic-web-usb.sh; host peer is 169.254.100.2.
# Never manages LAN1/eth0/eth1. Another nonempty IPv4 address is a conflict.
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LC_ALL=C
export PATH LC_ALL
# PUBLIC EXAMPLE: review firmware paths, USB identity and sample addresses first.
# Deliberately inert unless the installer explicitly acknowledges that review.
if [ "${BMC_USB_ADAPTER_REVIEWED:-0}" != 1 ]; then
    printf '%s\n' 'Example adapter disabled: review and explicitly set BMC_USB_ADAPTER_REVIEWED=1' >&2
    exit 1
fi
umask 077
SSH_CONFIG=/conf/ssh_server_config
SSH_CONFIG_LINK=/etc/ssh/sshd_config
SSH_OLD_CONFIG=/conf/sshd_config
SSH_DEFAULTS=/etc/default/ssh
SSH_SERVICE_CONFIG=/conf/ncml.conf

report() {
    if command -v logger >/dev/null 2>&1; then
        logger -t sonic-web-usb "$1"
    else
        printf '%s\n' "$1" >&2
    fi
}

[ -d /sys/class/net/usb0 ] || exit 0
snapshot=$(ifconfig usb0 2>/dev/null) || {
    report 'Unable to read usb0; no network changes made'
    exit 1
}
address=$(printf '%s\n' "$snapshot" | sed -n 's/.*inet addr:\([0-9.]*\).*/\1/p')
if [ -z "$address" ] && printf '%s\n' "$snapshot" | grep -q 'inet[[:space:]]'; then
    report 'Unrecognized usb0 IPv4 output; refusing to change the interface'
    exit 1
fi
case "$address" in
    ''|169.254.100.1) ;;
    *) report 'usb0 has an unexpected IPv4 address; refusing to overwrite it'; exit 1 ;;
esac
if [ "$address" != 169.254.100.1 ] ||
   ! printf '%s\n' "$snapshot" | grep -q 'Mask:255\.255\.255\.252' ||
   ! printf '%s\n' "$snapshot" | grep -q '[[:space:]]UP[[:space:]]'; then
    # No gateway, DNS, bridge, forwarding or physical-LAN changes.
    ifconfig usb0 169.254.100.1 netmask 255.255.255.252 up || {
        report 'Failed to configure the private USB link'
        exit 1
    }
    snapshot=$(ifconfig usb0 2>/dev/null) || exit 1
    address=$(printf '%s\n' "$snapshot" | sed -n 's/.*inet addr:\([0-9.]*\).*/\1/p')
    if [ "$address" != 169.254.100.1 ] ||
       ! printf '%s\n' "$snapshot" | grep -q 'Mask:255\.255\.255\.252' ||
       ! printf '%s\n' "$snapshot" | grep -q '[[:space:]]UP[[:space:]]'; then
        report 'usb0 readback did not confirm the expected address, mask and UP state'
        exit 1
    fi
    report 'Configured private usb0 link at 169.254.100.1/30'
fi

# A late USB gadget can make the normal boot-time sshd bind fail. Only start
# the existing SSH service if no port-22 listener exists and its effective
# configuration still binds exclusively to this private address.
read_ssh_listeners() {
    listeners=$(netstat -lnt 2>/dev/null) || return 1
    ssh_listeners=$(printf '%s\n' "$listeners" |
        awk '$1 ~ /^tcp/ && $4 ~ /:22$/ && $6 == "LISTEN" { print $4 }')
}
read_ssh_listeners || {
    report 'Unable to read TCP listeners; refusing to start SSH'
    exit 1
}
case "$ssh_listeners" in
    169.254.100.1:22) exit 0 ;;
    '') ;;
    *) report 'Unexpected port-22 listener; refusing to start or restart SSH'; exit 1 ;;
esac
# The verified AMI start script has a migration branch which can replace
# ssh_server_config with vendor defaults. Never trigger that branch here.
if [ -L "$SSH_OLD_CONFIG" ] || [ ! -f "$SSH_CONFIG" ] ||
   [ -L "$SSH_CONFIG" ] || [ ! -L "$SSH_CONFIG_LINK" ] ||
   [ "$(readlink "$SSH_CONFIG_LINK")" != "$SSH_CONFIG" ]; then
    report 'SSH configuration layout changed; refusing the vendor migration path'
    exit 1
fi
defaults=$(sed '/^[[:space:]]*#/d; /^[[:space:]]*$/d' "$SSH_DEFAULTS" 2>/dev/null) || exit 1
case "$defaults" in
    'SSHD_OPTS='|'SSHD_OPTS=""'|"SSHD_OPTS=''") ;;
    *) report 'Unexpected SSH startup options; service not started'; exit 1 ;;
esac
read_ssh_service_value() {
    awk -F= -v key="$1" '
        /^\[ssh\][[:space:]]*$/ { in_ssh=1; next }
        /^\[/ { in_ssh=0 }
        in_ssh { name=$1; value=$2; gsub(/[[:space:]]/, "", name); gsub(/[[:space:]]/, "", value);
                 if (name == key) print value }
    ' "$SSH_SERVICE_CONFIG"
}
if [ "$(read_ssh_service_value secure_port)" != 22 ] ||
   [ "$(read_ssh_service_value current_state)" != 1 ]; then
    report 'SSH service state or command-line port changed; service not started'
    exit 1
fi
/usr/sbin/sshd -t -f "$SSH_CONFIG" >/dev/null 2>&1 || {
    report 'SSH configuration validation failed; service not started'
    exit 1
}
effective=$(/usr/sbin/sshd -T -f "$SSH_CONFIG" 2>/dev/null) || {
    report 'Unable to read effective SSH configuration; service not started'
    exit 1
}
bindings=$(printf '%s\n' "$effective" | awk '$1 == "listenaddress" { print $2 }')
if [ "$bindings" != 169.254.100.1:22 ]; then
    report 'SSH configuration does not exclusively bind the private USB address; service not started'
    exit 1
fi
/etc/init.d/ssh start >/dev/null 2>&1 || {
    report 'Existing SSH service start failed'
    exit 1
}
read_ssh_listeners || exit 1
if [ "$ssh_listeners" != 169.254.100.1:22 ]; then
    report 'SSH readback did not confirm the private port-22 listener'
    exit 1
fi
report 'Started existing SSH service on the private USB link'
