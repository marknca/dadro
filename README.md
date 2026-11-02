# DADRO

Keep your YADRO hardware running with this modern re-write of Nick Miller's YADRO software.

## Requirements

This re-write was designed to run on Linux, macOS, and Windows. It's written using the Python programming language and uses a framework called QT for the user interface. Both of these are freely available for this use case on a broad set of supported platforms.

Most users will be running this on a Linux distribution given the primary use case of running on a shop environment. For Raspberry Pi systems, the following command will install Python and QT:

```bash
sudo apt install python3 python3-qt6 qt6-wayland
```

Distributions using other package managers—like `yum`—should have these packages available using the same or similar names. You're looking for Python version 3 or higher and QT version 6 or higher.


On macOS, use Homebrew to get up and running quickly with:

```bash
brew install python3 
brew install pyqt
```

Once you have Python and QT installed on any system, add the final Python libraries with:

```bash
pip3 install -r requirements.txt
```

## Running the program

```bash
python3 release/src/app.py
```
