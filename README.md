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

## History

My first introduction to the YADRO project was in 2008. My father bought the hardware kit to setup scales on his 3-axis mill. Since then, he added a YADRO kit to his lathe and has been using the hardware+software regularly with minimal issue for over 18 years.

The YADRO project was launched in 2006 and went offline sometime in the fall of 2014. The Wayback Machine from the Internet Archive has a number of snapshots from when the project was active, but [this last one](https://web.archive.org/web/20140930031902/https://yadro.de/) is probably the most useful.

> You can navigate the original project website through [the Wayback Machine](https://web.archive.org/web/20140930031902/https://yadro.de/) or via the cached copy in this repo under [yadro-de](/yadro-de).

The tl:dr of the project is that it's small hardware device that reads and powers up to 4 scale inputs (3 axes + 1 quill). Paired with it's DOS-based software, it provides a simple, robust digital read out (DRO) for lathes and mills.

### Original software

My father's original setup was using an old 486sx latop with a TFT screen and running the software off of a bootable 3.5" floppy disk. Believe it or not, this was a reasonably reliable setup. It worked for years and when—once or twice—there was an inevitable hardware failure, a replacement laptop was easy enough to get from the local electronics recycler for under $80 CDN.

As you might guess, a major challenge was the floppy disk. A fragile, spinning PET plastic disk with a ferromagnetic oxide coating that's read by two electromagnetic heads is **not** a piece of equipment that you want running next to a lathe or a mill!

All of those little metal shavings eventually find their way into either the laptop, the floppy disk drive, or the actual floppy disk.

> If you are still using a floppy disk-based system, there is an image file and the full boot disk contents in this repo at [/original-bootable-floppy](/original-bootable-floppy)

### Original hardware

Nick's website has a complete breakdown of the hardware. From how to [build the PCB](/yadro-de/building-the-kit.html) to the [complete circuit diagram](/yadro-de/int4.pdf) to how to [debug the scales](/yadro-de/debugging-scales.html). There is also a complete schematic for [the wire protocol](/yadro-de/in-detail-interface-dro-int-4.html) that the software interprets.

The hardware is extremely well designed and the only reason to consider replacing it is if your current scales fail and you decide to go in a different direction with their replacements.

## Modernizing the project

To replace the floppy disk-based system, I first tried to virtualize the DOS software on top of Linux. Specifically, I tried to get it running effeciently under [DOSBox-X](https://dosbox-x.com), [DOSEMU2](https://dosemu2.github.io/dosemu2/), and [86Box](https://86box.net).

Each of these was capable of running the basic software, but inevitably ran into challenges with "fast mode" where the reads from the serial port were near-constant. 

Why? This is a question I wrestled with for a while. Even the smallest of [Raspberry Pi Zero](https://www.raspberrypi.com/documentation/computers/zero.html) absolutely eclipses the 486 hardware the project started on. Even with the overhead of running a native operating system (Linux) and then emulating another (DOS) while converting CPU instructions from x86 to ARM should leave enough computing power to run hundreds of DRO instances.

But things rarely work the way the spec says.

At the heart of the matter is the challenge with the serial port over USB adapters that are inevitably required to connect to the YADRO hardware. This adds some additional hardware limitations (the adapters are not designed with this use case in mind) that conflicted with the original DOS programming.

It is possible to get modern replacements to create a direct serial port connection wired to a Raspberry Pi, but that didn't seem worth the effort. 

There were just too many moving pieces to ensure this was a reliable implementation.

### Reverse engineering the original software

With the complete documentation and working code, this project started by directing OpenAI's Codex using GPT6-Astra to reverse engineering and document the complete functionality of the existing YADRO software.

That work resulted in a quite comprehensive set of documents describing how the hardware and DOS application interacted and how the work was done. The key information from that effort has been cleaned up and imported here as [/docs/yadro-dos-commands.html](/docs/yadro-dos-commands.html). This page lists all of the basic and macro commands available in the original software and explains what they do.

That document is the basis for the new application code. Since we had a reference that was built from and proven by the original code, we were more confident in the outcome of a rewrite.

This was enough that we were able to write a quite proof of concept by hand. This quick test confirmed that the issue was the DOS-based software and the serial-to-USB adapter, the new code easily read at full speed from the YADRO through a serial-to-USB adapter with minimal hardware. 

### Rewriting the code and updating the user interface

The protocol to interpret the data from the YADRO hardware was the easiest part of the project. With complete documentation and the reverse engineered original software, Codex was able to write a new parser in Python that required only minor adjustments after testing with real hardware ([/release/src/yadro_protocol.py](/release/src/yadro_protocol.py)).

Efforts then moved to modernizing the user interface. While the original [DOS interface](/yadro-de/dro-screen-1.gif) now comes across as very _retro_, it's doesn't provide a great experience on modern hardware.

The new implementation is designed with a more modern 1920x1080 resolution (1080p) in mind. It'll scale and adjust to other resolutions, but the expectation is that most low-cost displays will be operating at this resolution. By aiming for this common 16:9 aspect ratio, the interface focuses on displaying the scale readings is a clean, bold control that you can see clearly from a distance.

Multiple macro from the original YADRO have been condensed into more intuitive functionality. This is now possible because the software is working with a graphical user interface and not a simple text-based one. 

Simple keyboard navigation has been maintained and the entire application can be navigated with the four arrow keys, enter, and `/`...all conveniently located on a standard number pad.

The specifics of the new software are described in depth in the documentation. [You can start reading that here.](/docs/user-guide/index.html).