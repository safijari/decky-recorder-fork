import os
import sys
import traceback
import subprocess
import signal
import time
from datetime import datetime
from pathlib import Path
from settings import SettingsManager
import decky_plugin
import logging
import shutil
import json

# Get environment variable
settingsDir = os.environ["DECKY_PLUGIN_SETTINGS_DIR"]

import asyncio

DEPSPATH = Path(decky_plugin.DECKY_PLUGIN_DIR) / "bin"
if not DEPSPATH.exists():
    DEPSPATH = Path(decky_plugin.DECKY_PLUGIN_DIR) / "backend/out"
GSTPLUGINSPATH = DEPSPATH / "gstreamer-1.0"

std_out_file_path = Path(decky_plugin.DECKY_PLUGIN_LOG_DIR) / "decky-recorder-std-out.log"
std_out_file = open(std_out_file_path, "w")
std_err_file = open(Path(decky_plugin.DECKY_PLUGIN_LOG_DIR) / "decky-recorder-std-err.log", "w")

logger = decky_plugin.logger

from logging.handlers import TimedRotatingFileHandler

log_file = Path(decky_plugin.DECKY_PLUGIN_LOG_DIR) / "decky-recorder.log"
log_file_handler = TimedRotatingFileHandler(log_file, when="midnight", backupCount=2)
log_file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.handlers.clear()
logger.addHandler(log_file_handler)

try:
    sys.path = [str(DEPSPATH / "psutil")] + sys.path
    import psutil

    logger.info("Successfully loaded psutil")
except Exception:
    logger.info(traceback.format_exc())


def find_gst_processes():
    pids = []
    for child in psutil.process_iter():
        try:
            if "Decky-Recorder" in " ".join(child.cmdline()):
                pids.append(child.pid)
        except psutil.NoSuchProcess:
            pass
    return pids


def in_gamemode():
    for child in psutil.process_iter():
        try:
            if "gamescope-session" in " ".join(child.cmdline()):
                return True
        except psutil.NoSuchProcess:
            pass
    return False

def get_cmd_output(cmd):
    logger.info(f"Command: {cmd}")
    return subprocess.getoutput(cmd).strip()

def unload_pa_modules(search_string):
    module_list = get_cmd_output(f"pactl list short modules | grep '{search_string}' | awk '{{print $1}}'").split("\n")
    for module_id in module_list:
        get_cmd_output(f"pactl unload-module {module_id}")

class Plugin:
    _recording_process = None
    _filepath: str = None
    _mode: str = "localFile"
    _audioBitrate: int = 128
    _localFilePath: str = decky_plugin.HOME + "/Videos"
    _rollingRecordingFolder: str = "/dev/shm"
    _rollingRecordingPrefix: str = "Decky-Recorder-Rolling"
    _fileformat: str = "mkv"
    _rolling: bool = False
    _micEnabled: bool = False
    _micGain: float = 13.0
    _noiseReductionPercent: int = 50
    _micSource: str = "NA"
    _deckySinkModuleName: str = "Decky-Recording-Sink"
    _echoCancelledAudioName: str = "Echo-Cancelled-Audio"
    _echoCancelledMicName: str = "Echo-Cancelled-Mic"
    _optional_denoise_binary_path= decky_plugin.HOME + "/homebrew/data/decky-recorder/librnnoise_ladspa.so"
    _last_clip_time: float = time.time()
    _watchdog_task = None
    _muxer_map = {"mp4": "matroskamux", "mkv": "matroskamux", "mov": "qtmux"}
    _settings = None

    async def clear_rogue_gst_processes(self):
        gst_pids = find_gst_processes()
        curr_pid = self._recording_process.pid if self._recording_process is not None else None
        for pid in gst_pids:
            if pid != curr_pid:
                logger.info(f"Killing rogue process {pid}")
                os.kill(pid, signal.SIGKILL)

    async def watchdog(self):
        logger.info("Watchdog started")
        while True:
            try:
                in_gm = in_gamemode()
                is_cap = await Plugin.is_capturing(self, verbose=False)
                if not in_gm and is_cap:
                    await Plugin.stop_capturing(self)
                    await Plugin.clear_rogue_gst_processes(self)
                std_out_lines = open(std_out_file_path, "r").readlines()
                if std_out_lines:
                    is_cap = is_cap and ("Freeing" not in std_out_lines[-1])
                if not in_gm and is_cap:
                    logger.warn("Left gamemode but recording was still running, killing capture")
                    await Plugin.stop_capturing(self)
                # This can be buggy due to race condition between disabling rolling and the watchdog seeing that rolling is disabled
                elif in_gm and not is_cap and self._rolling:
                    # Add another 2 second wait to ensure that the state is still consistent...
                    await asyncio.sleep(2)
                    if self._rolling:
                        logger.warn("In gamemode but recording was not working, starting capture")
                        await Plugin.stop_capturing(self)
                        await Plugin.start_capturing(self)
            except Exception:
                logger.exception(f"watchdog exception! {Exception.message}")
            await asyncio.sleep(2)

    # Starts the capturing process
    async def start_capturing(self, app_name: str = ""):
        try:
            logger.info("Starting recording")

            app_name = str(app_name).replace(":", " ").replace("/", " ")
            if app_name == "" or app_name == "null":
                app_name = "Decky-Recorder"

            muxer = Plugin._muxer_map.get(self._fileformat, "matroskamux")
            logger.info(f"Starting recording for {self._fileformat} with mux {muxer}")
            if await Plugin.is_capturing(self) == True:
                logger.info("Error: Already recording")
                return

            await Plugin.clear_rogue_gst_processes(self)

            os.environ["XDG_RUNTIME_DIR"] = "/run/user/1000"
            os.environ["XDG_SESSION_TYPE"] = "wayland"
            os.environ["HOME"] = decky_plugin.DECKY_HOME

            # Start command including plugin path and ld_lib path
            start_command = (
                "GST_VAAPI_ALL_DRIVERS=1 GST_PLUGIN_PATH={} LD_LIBRARY_PATH={} gst-launch-1.0 -e -vvv".format(
                    str(GSTPLUGINSPATH), str(DEPSPATH)
                )
            )

            # Video Pipeline
            if not self._rolling:
                videoPipeline = f"pipewiresrc do-timestamp=true ! vaapipostproc ! queue ! vaapih264enc ! h264parse ! {muxer} name=sink !"
            else:
                videoPipeline = "pipewiresrc do-timestamp=true ! vaapipostproc ! queue ! vaapih264enc ! h264parse !"

            cmd = "{} {}".format(start_command, videoPipeline)

            # If mode is localFile
            if self._mode == "localFile":
                logger.info("Local File Recording")
                dateTime = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                if self._rolling:
                    logger.info("Setting tmp filepath")
                    self._filepath = (
                        f"{self._rollingRecordingFolder}/{self._rollingRecordingPrefix}_%02d.{self._fileformat}"
                    )
                if not self._rolling:
                    logger.info("Setting local filepath no rolling")
                    self._filepath = f"{self._localFilePath}/{app_name}_{dateTime}.{self._fileformat}"
                    fileSinkPipeline = f' filesink location="{self._filepath}" '
                else:
                    logger.info("Setting local filepath")
                    fileSinkPipeline = f" splitmuxsink name=sink muxer={muxer} muxer-pad-map=x-pad-map,audio=vid location={self._filepath} max-size-time=1000000000 max-files=480"
                cmd = cmd + fileSinkPipeline
            else:
                logger.info(f"Mode {self._mode} does not exist")
                return

            deckyRecordingSinkExists = subprocess.run(f"pactl list sinks | grep '{self._deckySinkModuleName}'", shell=True).returncode == 0

            if deckyRecordingSinkExists:
                logger.info(f"{self._deckySinkModuleName} already exists, rebuilding sink for safety")
                await Plugin.cleanup_decky_pa_sink(self)

            await Plugin.create_decky_pa_sink(self)

            cmd = (
                cmd
                + f' pulsesrc device="{self._deckySinkModuleName}.monitor" ! audio/x-raw, channels=2 ! audioconvert ! lamemp3enc target=bitrate bitrate={self._audioBitrate} cbr=true ! sink.audio_0'
            )

            # Starts the capture process
            logger.info("Command: " + cmd)
            self._recording_process = subprocess.Popen(cmd, shell=True, stdout=std_out_file, stderr=std_err_file)
            logger.info("Recording started!")
        except Exception:
            await Plugin.stop_capturing(self)
            logger.info(traceback.format_exc())
        return

    # Stops the capturing process and cleans up if the mode requires
    async def stop_capturing(self):
        logger.info("Stopping recording")
        if await Plugin.is_capturing(self) == False:
            logger.info("Error: No recording process to stop")
            return
        logger.info("Sending sigin")
        proc = self._recording_process
        self._recording_process = None
        proc.send_signal(signal.SIGINT)
        logger.info("Sigin sent. Waiting...")
        try:
            proc.wait(timeout=10)
        except Exception:
            logger.warn("Could not interrupt gstreamer, killing instead")
            await Plugin.clear_rogue_gst_processes(self)
        logger.info("Waiting finished. Recording stopped!")

        await Plugin.cleanup_decky_pa_sink(self)
        return

    # Returns true if the plugin is currently capturing
    async def is_capturing(self, verbose=True):
        if verbose:
            logger.info("Is capturing? " + str(self._recording_process is not None))
        return self._recording_process is not None

    async def is_rolling(self):
        logger.info(f"Is Rolling? {self._rolling}")
        await Plugin.clear_rogue_gst_processes(self)
        return self._rolling

    async def enable_rolling(self):
        logger.info("Enable rolling was called begin")
        # if capturing, stop that capture, then re-enable with rolling
        if await Plugin.is_capturing(self):
            await Plugin.stop_capturing(self)
        self._rolling = True
        await Plugin.start_capturing(self)
        await Plugin.saveConfig(self)
        logger.info("Enable rolling was called end")

    async def disable_rolling(self):
        logger.info("Disable rolling was called begin")
        # turn rolling off ASAP to avoid race condition with watchdog
        self._rolling = False
        if await Plugin.is_capturing(self):
            await Plugin.stop_capturing(self)
        await Plugin.saveConfig(self)
        try:
            for path in list(Path(self._rollingRecordingFolder).glob(f"{self._rollingRecordingPrefix}*")):
                os.remove(str(path))
            logger.info("Deleted all files in rolling buffer")
        except Exception:
            logger.exception("Failed to delete rolling recording buffer files")
        logger.info("Disable rolling was called end")

    async def create_decky_pa_sink(self):
        logger.info("Making audio pipeline")
        # Creates audio pipeline
        audio_device_output = get_cmd_output("pactl get-default-sink")
        # expected output: alsa_output.pci-0000_04_00.5-platform-acp5x_mach.0.HiFi__hw_acp5x_1__sink when using internal speaker
        # bluez_output.20_74_CF_F1_C0_1E.1 when using bluetooth
        logger.info(f"Creating {self._deckySinkModuleName}")

        get_cmd_output(f"pactl load-module module-null-sink sink_name={self._deckySinkModuleName}")

        get_cmd_output(f"pactl load-module module-loopback source={audio_device_output}.monitor sink={self._deckySinkModuleName}")

        if await Plugin.is_mic_enabled(self):
            await Plugin.attach_mic(self)

    async def cleanup_decky_pa_sink(self):
        unload_pa_modules("Echo-Cancelled")
        unload_pa_modules(f"{self._deckySinkModuleName}")

    async def get_default_mic(self):
        return get_cmd_output("pactl get-default-source")

    async def is_mic_enabled(self):
        logger.info(f"Is mic enabled? {self._micEnabled}")
        return self._micEnabled

    async def is_mic_attached(self):
        is_attached = subprocess.run("pactl list modules | grep 'Echo-Cancelled'",shell=True).returncode == 0
        logger.info(f"Is mic attached? {is_attached}")
        return is_attached

    async def attach_mic(self):
        logger.info(f"Attaching Microphone {self._echoCancelledMicName}")

        if self._micSource == "NA":
            self._micSource = await Plugin.get_default_mic(self)

        # check if the user has downloaded the optional noise cancellation binary
        if await Plugin.enhanced_noise_binary_exists(self):
            # attached echo cancelled mic

            get_cmd_output(f"pactl load-module module-null-sink sink_name={self._echoCancelledMicName} rate=48000")

            get_cmd_output(f"pactl load-module module-ladspa-sink sink_name={self._echoCancelledMicName}_raw_in sink_master={self._echoCancelledMicName} label=noise_suppressor_mono plugin={self._optional_denoise_binary_path} control={self._noiseReductionPercent},20,0,0,0")

            # This module cannot use @DEFAULT_SOURCE@, don't know why
            get_cmd_output(f"pactl load-module module-loopback source={self._micSource} sink={self._echoCancelledMicName}_raw_in channels=1 source_dont_move=true sink_dont_move=true")

            get_cmd_output(f"pactl set-source-volume {self._echoCancelledMicName}.monitor {self._micGain}db")

            get_cmd_output(f"pactl load-module module-loopback source={self._echoCancelledMicName}.monitor sink={self._deckySinkModuleName}")
        else:
            get_cmd_output(f"pactl load-module module-echo-cancel use_master_format=1 source_master={self._micSource} sink_master=@DEFAULT_SINK@ source_name={self._echoCancelledMicName} sink_name={self._echoCancelledAudioName} aec_method='webrtc' aec_args='analog_gain_control=0 digital_gain_control=1'")
            get_cmd_output(f"pactl set-source-volume Echo-Cancelled-Mic {self._micGain}db")
            get_cmd_output(f"pactl load-module module-loopback source={self._echoCancelledMicName} sink={self._deckySinkModuleName}")
            get_cmd_output(f"pactl load-module module-loopback source={self._echoCancelledAudioName}.monitor sink={self._deckySinkModuleName}")

    async def detach_mic(self):
        logger.info(f"Detaching Microphone {self._echoCancelledMicName}")
        unload_pa_modules("Echo-Cancelled")

    async def enable_microphone(self):
        logger.info("Enable microphone")
        if await Plugin.is_capturing(self):
            if not await Plugin.is_mic_attached(self):
                await Plugin.attach_mic(self)
        self._micEnabled = True
        await Plugin.saveConfig(self)
        logger.info("Enable mic was called end")

    async def disable_microphone(self):
        logger.info("Disable microphone")
        # if capturing, stop that capture, then re-enable with rolling
        if await Plugin.is_capturing(self):
            if await Plugin.is_mic_attached(self):
                await Plugin.detach_mic(self)
        self._micEnabled = False   
        await Plugin.saveConfig(self)
        logger.info("Disable mic was called end")
    
    async def get_mic_gain(self):
        return self._micGain

    async def update_mic_gain(self, new_gain: float):
        self._micGain = float(new_gain)
        if await Plugin.is_capturing(self):
            if await Plugin.is_mic_attached(self):
                get_cmd_output(f"pactl set-source-volume Echo-Cancelled-Mic {self._micGain}db")
        await Plugin.saveConfig(self)

    async def enhanced_noise_binary_exists(self):
        return os.path.exists(self._optional_denoise_binary_path)

    async def get_noise_reduction_percent(self):
        return self._noiseReductionPercent

    async def update_noise_reduction_percent(self, new_percent: int):
        logger.info(f"Updating noise reduction percent {new_percent}")
        self._noiseReductionPercent = int(new_percent)
        if await Plugin.is_capturing(self):
            if await Plugin.is_mic_enabled(self):
                await Plugin.detach_mic(self)
                await Plugin.attach_mic(self)
        await Plugin.saveConfig(self)

    async def get_mic_source(self):
        return self._micSource

    async def get_mic_sources(self):
        logger.info(f"Getting available mic sources")
        raw_sources = get_cmd_output("pactl list short sources | awk '{print $2}'").split("\n")
        default_source = await Plugin.get_default_mic(self)
        sources_json = [{"data": f"{default_source}", "label": "Default Mic"}]
        for source in raw_sources:
            # Stop recursive pointing
            if "Echo" not in source and "monitor" not in source and "Decky" not in source and source != default_source:
                sources_json.append({"data": source, "label": source})

        logger.info(json.dumps(sources_json))
        return json.dumps(sources_json)

    async def set_mic_source(self, new_mic_source: str):
        logger.info(f"Setting new mic source: {new_mic_source}")
        self._micSource = new_mic_source
        if await Plugin.is_capturing(self):
            if await Plugin.is_mic_enabled(self):
                await Plugin.detach_mic(self)
                await Plugin.attach_mic(self)

    # Sets the current mode, supported modes are: localFile
    async def set_current_mode(self, mode: str):
        logger.info("New mode: " + mode)
        self._mode = mode

    # Gets the current mode
    async def get_current_mode(self):
        logger.info("Current mode: " + self._mode)
        return self._mode

    # Sets audio bitrate
    async def set_audio_bitrate(self, audioBitrate: int):
        logger.info(f"New audio bitrate: {audioBitrate}")
        self._audioBitrate = audioBitrate

    # Gets the audio bitrate
    async def get_audio_bitrate(self):
        logger.info("Current audio bitrate: " + self._audioBitrate)
        return self._audioBitrate

    # Sets local FilePath
    async def set_local_filepath(self, localFilePath: str):
        logger.info("New local filepath: " + localFilePath)
        self._localFilePath = localFilePath
        await Plugin.saveConfig(self)

    # Gets the local FilePath
    async def get_local_filepath(self):
        logger.info("Current local filepath: " + self._localFilePath)
        return self._localFilePath

    # Sets local file format
    async def set_local_fileformat(self, fileformat: str):
        logger.info("New local file format: " + fileformat)
        self._fileformat = fileformat
        await Plugin.saveConfig(self)

    # Gets the file format
    async def get_local_fileformat(self):
        logger.info("Current local file format: " + self._fileformat)
        return self._fileformat

    async def loadConfig(self):
        logger.info("Loading settings from: {}".format(os.path.join(settingsDir, "decky-loader-settings.json")))
        ### TODO: IMPLEMENT ###
        self._settings = SettingsManager(name="decky-loader-settings", settings_directory=settingsDir)
        self._settings.read()
        self._mode = "localFile"
        self._audioBitrate = 192000

        self._localFilePath = self._settings.getSetting("output_folder", decky_plugin.DECKY_HOME + "/Videos")
        self._fileformat = self._settings.getSetting("format", "mkv")
        self._rolling = self._settings.getSetting("rolling", False)
        self._micEnabled = self._settings.getSetting("mic_enabled", False)
        self._micGain = self._settings.getSetting("mic_gain", 13.0)
        self._noiseReductionPercent = self._settings.getSetting("noise_reduction_percent", 50.0)

        # Need this for initialization only honestly
        await Plugin.saveConfig(self)
        return

    async def saveConfig(self):
        logger.info("Saving config")
        self._settings.setSetting("format", self._fileformat)
        self._settings.setSetting("output_folder", self._localFilePath)
        self._settings.setSetting("rolling", self._rolling)
        self._settings.setSetting("mic_enabled", self._micEnabled)
        self._settings.setSetting("mic_gain", self._micGain)
        self._settings.setSetting("noise_reduction_percent", self._noiseReductionPercent )

        return

    async def _main(self):
        loop = asyncio.get_event_loop()
        self._watchdog_task = loop.create_task(Plugin.watchdog(self))
        await Plugin.loadConfig(self)
        if await Plugin.is_rolling(self):
            # Prevent bug where pulseaudio is not yet ready on fresh restart
            logger.info("Waiting 5 seconds before starting Decky Recorder")
            await asyncio.sleep(5)
            await Plugin.start_capturing(self)
        return

    async def _unload(self):
        logger.info("Unload was called")
        if await Plugin.is_capturing(self) == True:
            logger.info("Cleaning up")
            await Plugin.stop_capturing(self)
            await Plugin.saveConfig(self)
        return

    async def save_rolling_recording(self, clip_duration: float = 30.0, app_name: str = ""):
        app_name = str(app_name).replace(":", " ").replace("/", " ")
        if app_name == "" or app_name == "null":
            app_name = "Decky-Recorder"
        clip_duration = int(clip_duration)
        logger.info("Called save rolling function")

        if not await Plugin.is_capturing(self):
            logger.warn("Tried to capture recording, but capture was not started!")
            await Plugin.start_capturing(self)
            return -1

        if time.time() - self._last_clip_time < 2:
            logger.info("Too early to record another clip")
            return 0
        try:
            clip_duration = float(clip_duration)
            files = list(Path(self._rollingRecordingFolder).glob(f"{self._rollingRecordingPrefix}*.{self._fileformat}"))
            times = [os.path.getctime(p) for p in files]
            ft = sorted(zip(files, times), key=lambda x: -x[1])
            max_time = time.time()
            files_to_stitch = []
            actual_dur = 0.0
            for f, ftime in ft:
                if max_time - ftime <= clip_duration:
                    actual_dur = max_time - ftime
                    files_to_stitch.append(f)
            with open(self._rollingRecordingFolder + "/files", "w") as ff:
                for f in reversed(files_to_stitch):
                    ff.write(f"file {str(f)}\n")

            dateTime = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            ffmpeg = subprocess.Popen(
                f'ffmpeg -hwaccel vaapi -hwaccel_output_format vaapi -vaapi_device /dev/dri/renderD128 -f concat -safe 0 -i {self._rollingRecordingFolder}/files -c copy "{self._localFilePath}/{app_name}-{clip_duration}s-{dateTime}.{self._fileformat}"',
                shell=True,
                stdout=std_out_file,
                stderr=std_err_file,
            )
            ffmpeg.wait()
            os.remove(self._rollingRecordingFolder + "/files")
            self._last_clip_time = time.time()
            logger.info("finish save rolling function")
            return int(actual_dur)
        except Exception:
            logger.info(traceback.format_exc())
        return -1
