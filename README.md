# ish 2.0.0

ish opens a new command line in the terminal window you already use. Your usual shell handles typing, Tab completion, and command history. The screen starts plain. You can add shortcuts to scroll back or search earlier output.

## Start ish

The download is for 64-bit Intel or AMD computers running Linux. On Windows, use your Linux tab (such as Ubuntu) in Windows Terminal with Windows Subsystem for Linux (WSL). ish uses a shell already installed on your computer, such as Bash, zsh, or tcsh.

1. Look for `ish-2.0.0-linux-x86_64.tar.gz` under **Assets** on the [GitHub Releases page](https://github.com/E1der0wL/ish/releases). If 2.0.0 is not listed yet, the [build guide](docs/DEVELOPMENT.md) explains how to run this version from source.
2. Save the archive in your Linux home folder. On WSL, run `cd ~` and then `explorer.exe .` to open that folder in File Explorer and copy the download there.
3. In your Linux terminal, enter these commands one line at a time.

   ```sh
   cd ~
   tar -xzf ish-2.0.0-linux-x86_64.tar.gz
   cd ish-2.0.0-linux-x86_64
   ./ish
   ```

Your usual prompt appears. Try `pwd` and press Enter to see which folder you are in. To leave ish and return to the terminal you started from, type `exit` and press Enter. Keep the extracted folder together. Next time, open that folder in your terminal and run `./ish` again.

## Help and settings

Run these commands from the extracted folder.

| What you want to do | Command |
| --- | --- |
| Open the built-in help | `./ish --help` |
| Change the language, colors, or plugins | `./ish --config` |
| Start once with all plugins off | `./ish --no-plugins` |

In Settings, click a category on the left, change a setting, and select **Save**. Your next ish session will use the saved settings. Click **×** in the top bar to close Help or Settings.

## Search earlier output

To try the search plugin for one session, run this from the extracted folder.

```sh
./ish --plugin-dir bundles/plugins --plugin terminal_search
```

Type `pwd` and press Enter. Press Ctrl+F, type part of the folder name you saw, and press Enter to find it. Press Esc to return to the shell. The download also includes [plugins](bundles/plugins) for scrolling and jumping between commands, and a [module](bundles/modules) for clipboard access.

More guides (in Korean) cover [using ish](docs/VT_USAGE.md), [writing a plugin](docs/PLUGINS.md), and [what gets tested](docs/VT_TEST_CONTRACTS.md).
