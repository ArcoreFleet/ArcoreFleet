#!/bin/sh
# Reuse the existing hourly collector report; do not query the database or alter its service.
set -eu
report=/home/ubuntu/Projects/Tools/Monitor/DB_SUMMARY.md
test -r "$report"
script_dir="$HOME/.local/lib/fleet-metrics"
textfile_dir="$HOME/.local/share/fleet-metrics/textfile"
mkdir -p "$script_dir" "$textfile_dir"
chmod 755 "$textfile_dir"
cp "$(dirname "$0")/export_oracle_report.py" "$script_dir/export_oracle_report.py"
python3 "$script_dir/export_oracle_report.py" --input "$report" --output "$textfile_dir/oracle_db.prom"
sudo -n tee /etc/systemd/system/fleet-oracle-report.service >/dev/null <<EOF
[Unit]
Description=Publish sanitized existing Oracle storage report for Fleet

[Service]
Type=oneshot
User=ubuntu
ExecStart=/usr/bin/python3 $script_dir/export_oracle_report.py --input $report --output $textfile_dir/oracle_db.prom
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$textfile_dir
MemoryMax=64M
CPUQuota=10%
EOF
sudo -n tee /etc/systemd/system/fleet-oracle-report.timer >/dev/null <<'EOF'
[Unit]
Description=Refresh Fleet's cached Oracle report export

[Timer]
OnBootSec=30s
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
EOF
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now fleet-oracle-report.timer
sudo -n systemctl start fleet-oracle-report.service
echo 'Oracle report adapter enabled; existing srv-metrics service/timer unchanged.'
