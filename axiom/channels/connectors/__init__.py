"""Коннекторы к внешним каналам (мессенджеры и соцсети), помимо Telegram/WhatsApp.

Каждый модуль — один канал, с единым интерфейсом test_connection()/send_message().
Настройки (токены, кабинет) хранятся в app_settings под ключом connector_<id>,
см. axiom/web/app.py: /api/connectors/*.
"""
