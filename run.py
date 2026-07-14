#!/usr/bin/env python3
"""AI Switch — Unified AI API Key & Backend Management.

Supports:
  - Multi-vendor, multi-key API key management
  - Backend adapters for AI gateways and agent platforms
  - Health checking, batch import, auto-sync
  - Plugin architecture for extensibility
"""
from app import app

if __name__ == "__main__":
    app.run()
