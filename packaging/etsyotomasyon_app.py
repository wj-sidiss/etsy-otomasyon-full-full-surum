"""Entry point PyInstaller freezes into the downloadable app.

No arguments starts the web app and opens it in the browser; any arguments run the
command line tool.
"""

from etsyotomasyon.desktop import main

if __name__ == "__main__":
    main()
