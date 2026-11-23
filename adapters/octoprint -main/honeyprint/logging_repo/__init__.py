"""Logging Repository for HoneyPrint.

Analogous to HoneySat's Logging Repository, this module provides persistent
interaction logging backed by MongoDB. All honeypot interactions -- web requests,
Telnet sessions, login attempts, G-code commands, file uploads, and system
events -- are captured and stored for later analysis.
"""
