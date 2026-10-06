@echo off
rem Запуск парсера: run.bat [аргументы, напр. --limit 20]
cd /d %~dp0
python -m ru_iptv %*
