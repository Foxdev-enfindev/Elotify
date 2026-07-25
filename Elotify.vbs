Set WshShell = CreateObject("WScript.Shell")
' Lance le script .bat original en mode totalement masqué (le paramètre 0 cache la fenêtre)
WshShell.Run "Elotify.bat", 0, False