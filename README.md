# Auto Alarm

A small alarm clock widget for the desktop. It plays a sound file of your choice when an alarm goes off.
The window is borderless and dark, and it stays on top of other windows. It sits in a corner of your screen and shows the time and your next alarm.

## Features

- **Two alarm types**
  - *At a time*: ring at a set time on the days you pick.
  - *Auto repeat*: ring every N minutes. The shortest interval is 0.1 minutes (6 seconds).
- **12-hour or 24-hour clock**, set in Settings. Alarms are stored in 24-hour form, so you can switch at any time.
- **Your own sound**: MP3, WAV, OGG, or FLAC.
- **Sound controls**: play length in seconds, volume, and a loop option.
- **Window controls**: opacity, always on top, and a Stop button on the ringing banner.
- **System tray** (Windows): hide the widget to the tray. Right-click the icon to show the widget, stop the sound, or quit.
- **Pop-up on ring**: the widget returns to view when an alarm rings, even from the tray or the taskbar.
- **Keeps Windows awake** while the app runs, so alarms fire on time.
- **Remembers your settings**, alarms, and window position.

## Requirements

- Python 3.9 or newer
- [pygame-ce](https://pypi.org/project/pygame-ce/)
- Tkinter (included with Python on Windows and macOS; on Linux, install `python3-tk`)

The tray icon, taskbar button, and keep-awake feature use Windows calls. On other systems, the app runs without them.

## Setup

```
python -m pip install -r requirements.txt
```

## Run

```
python auto_alarm.py
```

## Use

1. Click **⚙ Settings**, then **Choose sound…** and pick an audio file. Click **Test** to hear it.
2. Click **＋ Add alarm**.
3. Pick **At a time** or **Auto repeat**, set the values, and click **Save alarm**.
4. Click an alarm in the list to edit it. Use the **ON/OFF** button to pause it, or **✕** to delete it.
5. When an alarm rings, click **Stop** on the red banner. The sound also stops when the play time ends.

## Settings

| Setting | What it does |
| --- | --- |
| Choose sound… / Test | Pick the sound file and play it once. |
| Play for (seconds) | How long the sound plays. |
| Volume | Playback volume. |
| Opacity | Window transparency. Lower values make the widget clearer. |
| Use 24-hour clock | Switch between 24-hour and 12-hour (AM/PM) time. |
| Loop sound until time is up | Repeat the sound for the full play time. |
| Keep on top of other windows | Keep the widget above other windows. |
| Show the widget when an alarm rings | Bring the widget back when an alarm goes off. |
| Hide to tray when I close (✕) | Send the widget to the tray instead of quitting. |

## Config file

Settings and alarms are saved to `.auto_alarm.json` in your home folder. Delete this file to reset the app.

## Build a Windows .exe

You can package the app with [PyInstaller](https://pyinstaller.org/):

```
python -m pip install pyinstaller
python -m PyInstaller --onefile --windowed --name AutoAlarm auto_alarm.py
```

You can also build from the saved settings in the repo:

```
python -m PyInstaller AutoAlarm.spec
```

The finished file is `dist/AutoAlarm.exe`. When you start the .exe, it closes any other running copy, so only the newest one stays open.

## Notes

- Alarms ring only while the app is running. Closing to the tray keeps it running; quitting does not.
- Auto repeat timers restart each time you open the app.
