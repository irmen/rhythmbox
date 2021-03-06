import threading
import subprocess
import tkinter
import urllib.request
from tkinter import ttk
from collections import namedtuple
from typing import Union

from PIL import Image, ImageTk
from synthplayer.playback import Output
from synthplayer.sample import Sample, LevelMeter
try:
    import miniaudio
except ImportError:
    miniaudio = None


class AudioDecoder:
    """
    Reads streaming audio from an IceCast stream,
    decodes it using ffmpeg or miniaudio if possible,
    and plays it on the output sound device.

    We need two threads:
     1) main thread that spawns ffmpeg, reads radio stream data, and writes that to ffmpeg
     2) background thread that reads decoded audio data from ffmpeg and plays it

    """
    def __init__(self, icecast_client, song_title_callback=None, update_ui=None):
        self.client = icecast_client
        self.stream_title = "???"
        self.song_title_callback = song_title_callback
        self.update_ui = update_ui
        self.ffmpeg_process = None
        self._stop_playback = False

    def stop_playback(self):
        self._stop_playback = True
        ffmpeg = self.ffmpeg_process
        self.ffmpeg_process = None
        if ffmpeg:
            # ffmpeg.stdin.close()
            # ffmpeg.stdout.close()
            ffmpeg.kill()

    def _audio_playback(self, pcm_stream):
        # thread 3: audio playback
        levelmeter = LevelMeter()

        def played(sample):
            if self.client.stream_title != self.stream_title:
                self.stream_title = self.client.stream_title
                if self.song_title_callback:
                    self.song_title_callback(self.stream_title)
                else:
                    print("\n\nNew Song:", self.stream_title, "\n")
            levelmeter.update(sample)
            if self.update_ui:
                self.update_ui(levelmeter, None)
            else:
                levelmeter.print(60, True)

        with Output(mixing="sequential", frames_per_chunk=44100//4) as output:
            output.register_notify_played(played)
            while not self._stop_playback:
                try:
                    audio = pcm_stream.read(44100 * 2 * 2 // 20)
                    if not audio:
                        break
                except (IOError, ValueError):
                    break
                else:
                    if not self._stop_playback:
                        sample = Sample.from_raw_frames(audio, 2, 44100, 2)
                        output.play_sample(sample)

    def stream_radio(self):
        if not self.song_title_callback:
            print("\nStreaming Radio Station: ", self.client.station_name)
        if miniaudio and self.client.audio_format != miniaudio.FileFormat.UNKNOWN:
            self.update_ui(None, "decoder: MiniAudio ("+self.client.audio_format.name+")")
            self.use_miniaudio_decoding(self.client.audio_format)
            return
        self.update_ui(None, "decoder: ffmpeg")
        self.use_ffmpeg_decoding(None)   # TODO we don't know the non-miniaudio file format anymore... :(

    def use_ffmpeg_decoding(self, fmt):
        cmd = ["ffmpeg", "-v", "fatal", "-nostdin", "-i", "-"]
        if fmt:
            cmd.extend(["-f", fmt])
        # cmd.extend(["-af", "aresample=resampler=soxr"])     # enable this if your ffmpeg has sox hq resample
        cmd.extend(["-ar", "44100", "-ac", "2", "-acodec", "pcm_s16le", "-f", "s16le", "-"])
        self.ffmpeg_process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        audio_playback_thread = threading.Thread(target=self._audio_playback, args=[self.ffmpeg_process.stdout], daemon=True)
        audio_playback_thread.start()
        try:
            while not self._stop_playback:
                chunk = self.client.read(8192)
                if self.ffmpeg_process:
                    self.ffmpeg_process.stdin.write(chunk)
                else:
                    break
        except BrokenPipeError:
            pass
        except KeyboardInterrupt:
            pass
        finally:
            self.client.close()
            self.stop_playback()
            audio_playback_thread.join()
            if not self.song_title_callback:
                print("\n")

    def use_miniaudio_decoding(self, fmt):
        pcm_stream = MiniaudioDecoderPcmStream(fmt, self.client)
        self._audio_playback(pcm_stream)


class MiniaudioDecoderPcmStream(miniaudio.StreamableSource):
    def __init__(self, fmt, stream):
        self.pcm_stream = miniaudio.stream_any(stream, fmt, dither=miniaudio.DitherMode.TRIANGLE)

    def read(self, size):
        try:
            return self.pcm_stream.send(size)
        except StopIteration:
            return b""


class Internetradio(tkinter.Tk):
    StationDef = namedtuple("StationDef", ["station_name", "stream_name", "icon_url", "stream_url"])
    stations = [
        StationDef("Soma FM", "Groove Salad",
                   "http://somafm.com/img/groovesalad120.png",
                   "http://ice3.somafm.com/groovesalad-64-aac"),
        StationDef("Soma FM", "Secret Agent",
                   "http://somafm.com/img/secretagent120.jpg",
                   "http://ice3.somafm.com/secretagent-128-mp3"),
        StationDef("University of Calgary", "CJSW-FM",
                   "https://calgaryartsdevelopment.com/wp-content/uploads/2019/06/CJSW-logo.png",
                   "https://cjsw.leanstream.co/CJSWFM"),
        StationDef("Playtrance.com", "Trance",
                   "https://www.playtrance.com/static/playtrancev20thumb.jpg",
                   "http://live.playtrance.com:8000/playtrance-main.aac"),
    ]

    def __init__(self):
        super().__init__()
        self.wm_title("Internet radio")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.geometry("+200+80")
        self.station_buttons = []
        for station in self.stations:
            button = tkinter.Button(self, text="\n"+station.station_name + "\n" + station.stream_name+"\n", width=14, height=8)
            button.pack()
            self.station_buttons.append(button)
        self.quit_button = tkinter.Button(self, text="quit", command=lambda: self.destroy())
        self.quit_button.pack()
        self.stream_name_label = tkinter.Label(self, text="Choose a station")
        self.stream_name_label.pack()
        self.song_title_label = tkinter.Label(self, text="...")
        self.song_title_label.pack()
        self.decoder_label = tkinter.Label(self, text="...")
        self.decoder_label.pack()
        s = ttk.Style()
        s.theme_use("default")
        s.configure("TProgressbar", thickness=8)
        self.level_left = ttk.Progressbar(self, orient=tkinter.HORIZONTAL, length=200, maximum=61, mode="determinate")
        self.level_left.pack()
        self.level_right = ttk.Progressbar(self, orient=tkinter.HORIZONTAL, length=200, maximum=61, mode="determinate")
        self.level_right.pack()
        self.icyclient = None
        self.decoder = None
        self.play_thread = None
        self.after(100, self.load_button_icons)

    def load_button_icons(self):
        for index, station in enumerate(self.stations):
            try:
                with urllib.request.urlopen(station.icon_url) as image_data:
                    image = Image.open(image_data)
            except OSError:
                button = self.station_buttons[index]
                button.configure(command=lambda s=station: self.station_select(s))
            else:
                image.thumbnail((128, 128))
                button = self.station_buttons[index]
                button._tkimage = ImageTk.PhotoImage(image)
                button.configure(image=button._tkimage, command=lambda s=station: self.station_select(s), width=160, height=140)

    def station_select(self, station):
        for button in self.station_buttons:
            button.configure(background=self.quit_button.cget("bg"))
        self.station_buttons[self.stations.index(station)].configure(background="lime green")
        if self.play_thread:
            self.set_song_title("(switching streams...)")
            self.update()
            self.icyclient.close()
            self.decoder.stop_playback()
            self.decoder = None
            self.play_thread.join(timeout=2)
        self.stream_name_label.configure(text="{} | {}".format(station.station_name, station.stream_name))
        self.icyclient = miniaudio.IceCastClient(station.stream_url)
        self.decoder = AudioDecoder(self.icyclient, self.set_song_title, self.update_ui)
        self.set_song_title("...")
        self.play_thread = threading.Thread(target=self.decoder.stream_radio, daemon=True)
        self.play_thread.start()

    def set_song_title(self, title):
        self.song_title_label["text"] = title

    def update_ui(self, levelmeter, message):
        if self.decoder and levelmeter:
            self.level_left.configure(value=60+levelmeter.level_left)
            self.level_right.configure(value=60+levelmeter.level_right)
        if message:
            self.decoder_label["text"] = message


radio = Internetradio()
radio.mainloop()
