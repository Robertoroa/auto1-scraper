#!/bin/bash
# Inicia el tunnel de Cloudflare para el panel de Auto1
cloudflared tunnel --url http://localhost:8765 \
  --logfile /Users/roberto/Documents/auto1-scraper/logs/cloudflared.log
