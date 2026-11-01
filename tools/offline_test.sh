#!/bin/sh
# Cut the Orange Pi off from the internet (LAN, loopback, docker and multicast stay up) to test that
# automations keep working without the cloud. Runs on the Pi's host network namespace through a
# privileged container, because the CUI user has docker but no passwordless sudo.
#
#   sh offline_test.sh on [minutes]   block now; a watchdog container lifts the block after N min (default 15)
#   sh offline_test.sh off            lift the block now
#   sh offline_test.sh status
set -e
host() { docker run --rm --privileged --pid host --network host alpine:3.20 nsenter -t 1 -m -u -n -i sh -c "$1"; }

RULES_OFF='
iptables -D OUTPUT -j HH_OFFLINE 2>/dev/null; iptables -F HH_OFFLINE 2>/dev/null; iptables -X HH_OFFLINE 2>/dev/null;
ip6tables -D OUTPUT -j HH_OFFLINE 2>/dev/null; ip6tables -F HH_OFFLINE 2>/dev/null; ip6tables -X HH_OFFLINE 2>/dev/null; true'

RULES_ON='
iptables -N HH_OFFLINE;
for n in 127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 224.0.0.0/4 255.255.255.255/32; do iptables -A HH_OFFLINE -d $n -j RETURN; done;
iptables -A HH_OFFLINE -j REJECT;
iptables -I OUTPUT 1 -j HH_OFFLINE;
ip6tables -N HH_OFFLINE;
ip6tables -A HH_OFFLINE -d 2000::/3 -j REJECT;
ip6tables -I OUTPUT 1 -j HH_OFFLINE'

case "$1" in
  on)
    host "$RULES_OFF; $RULES_ON"
    docker rm -f hh-offline-watchdog >/dev/null 2>&1 || true
    docker run -d --rm --name hh-offline-watchdog --privileged --pid host --network host alpine:3.20 \
      nsenter -t 1 -m -u -n -i sh -c "sleep $(( ${2:-15} * 60 )); $RULES_OFF" >/dev/null
    echo "offline until $(date -d "+${2:-15} min" +%H:%M) at the latest"
    ;;
  off)
    host "$RULES_OFF"
    docker rm -f hh-offline-watchdog >/dev/null 2>&1 || true
    echo "online"
    ;;
  status)
    host "iptables -S HH_OFFLINE 2>/dev/null | tail -1 || echo no-block; curl -s -m 5 -o /dev/null -w 'internet http %{http_code}\n' https://www.baidu.com || echo 'internet unreachable'"
    ;;
  *) echo "usage: $0 on [minutes] | off | status"; exit 1 ;;
esac
