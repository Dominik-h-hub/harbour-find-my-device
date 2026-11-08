#!/bin/bash

# Simple start/stop helper for the iCloud bridge.
# Path to the bridge's virtual environment (override via environment variable or edit here)
VENV=${VENV:-$HOME/icloud_bridge_venv}
cd "$(dirname "$0")"

function start () {
    >icloud_bridge_console.log
    $VENV/bin/python icloud_bridge.py >> icloud_bridge_console.log 2>&1 &
    echo "iCloud bridge started ..."
}

function stop () {
   pkill -f icloud_bridge.py
   echo "iCloud bridge stopped ..."
}

case $1 in
    "start" )
        start;
        ;;
    "stop" )
        stop;
        ;;
    "restart" )
        stop;
        sleep 2;
        start;
        ;;
    "login" )
        $VENV/bin/python icloud_bridge.py --login;
        ;;
    "list" )
        $VENV/bin/python icloud_bridge.py --list;
        ;;
    * )
        echo "Usage: $0 {start|stop|restart|login|list}";
        ;;
esac
