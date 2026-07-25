@echo off
title Elotify K-Pop Launcher

echo Lancement de Spotify...
start spotify:

echo Demarrage de Flask...
start /B python app.py

echo Ouverture du jeu...
timeout /t 3 /nobreak > NUL
start http://127.0.0.1:5000
pause