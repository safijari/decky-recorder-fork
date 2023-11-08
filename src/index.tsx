import {
	ButtonItem,
	definePlugin,
	PanelSection,
	PanelSectionRow,
	ServerAPI,
	staticClasses,
	Dropdown,
	DropdownOption,
	SingleDropdownOption,
	Router,
	ToggleField
} from "decky-frontend-lib";

import {
	VFC,
	useState,
	useEffect
} from "react";

import { FaVideo } from "react-icons/fa";

abstract class Button {
	saveClip: Function;
	scanBlocked: boolean = false
	
	constructor(saveClipFunc: Function) {
		this.saveClip = saveClipFunc;
	}

	blockScanning = async (timeout: number) => {
		this.scanBlocked = true;
		setTimeout(() => {
			this.scanBlocked = false;
		}, timeout)	
	}

 	abstract handleButtonInput(val: any[]) : any;
}

class CombinedButtons extends Button {
	pressedAt: number = Date.now();
	buttonVals: number[];

	constructor(buttonIndices: number[], saveClipFunc: Function) {
		super(saveClipFunc);
		this.buttonVals = buttonIndices.map((index) => (1 << index))
	}

	allButtonsDown = (buttons: number) => {
		for(var buttonVal of this.buttonVals) {
			if(!(buttons & buttonVal)) {
				return false;
			}
		}
		return true;
	}

	handleButtonInput = async (val: any[]) => {
		if (this.scanBlocked === true) {
			return;
		}
		for (const inputs of val) {
			if (inputs.ulButtons && this.allButtonsDown(inputs.ulButtons)) {
				this.blockScanning(2000);
				(Router as any).DisableHomeAndQuickAccessButtons();
				setTimeout(() => {
					(Router as any).EnableHomeAndQuickAccessButtons();
				}, 1000)
				await this.saveClip(30);
			}
		}
	}
}

class HoldDownButton extends Button {
	buttonVal: number;
	wasDown: number = 0; // each bit stands for a controller
	clipSaved: boolean = false;

	constructor(buttonIndex: number, saveClipFunc: Function) {
		super(saveClipFunc);
		this.buttonVal = (1 << buttonIndex);
	}

	checkHoldPerController = async (inputs: any, index: number) => {
		if ((inputs.ulButtons) && (inputs.ulButtons & this.buttonVal)) {
			if (this.scanBlocked === false) {	
				// not a new down, so it's a long press since we had a block sleep before,
				// save a clip if we haven't done so
				if (this.wasDown & (1 << index)) {
					if (this.clipSaved === false) {
						this.clipSaved = true;
						// block for 5 seconds to avoid smashing
						this.blockScanning(5000);
						this.saveClip();
					}
				// new down, block for some time and check again
				// if the button is still down
				} else { 
					this.wasDown = this.wasDown | (1 << index)
					this.clipSaved = false;
					this.blockScanning(500);	
				}
			}
		} else { 
			// target button was down but now up / released, 
			// reset status
			if (this.wasDown & (1 << index)) {
				this.wasDown = this.wasDown & ~(1 << index)
			}
		}
	}

	handleButtonInput = async (val: any[]) => {
		val.forEach(this.checkHoldPerController);
	}
}

class DeckyRecorderLogic {
	serverAPI: ServerAPI;

	constructor(serverAPI: ServerAPI) {
		this.serverAPI = serverAPI;
	}

	notify = async (message: string, duration: number = 1000, body: string = "") => {
		if (!body) {
			body = message;
		}
		await this.serverAPI.toaster.toast({
			title: message,
			body: body,
			duration: duration,
			critical: true
		});
	}

	saveRollingRecording = async (duration: number) => {
		// rolling recording wasn't started, start from now on
		const isRolling = await this.serverAPI.callPluginMethod("is_rolling", {});
		if ((isRolling.result as boolean) === false) {
			await this.notify("Enabling replay mode", 1500, "Steam + Start to save last 30 seconds");
			this.toggleRolling(false);
			return;
		}

		const res = await this.serverAPI.callPluginMethod('save_rolling_recording', { clip_duration: duration, app_name: Router.MainRunningApp?.display_name});
		let r = (res.result as number)
		if (r > 0) {
			await this.notify("Saved clip");
		} else if (r == 0) {
			await this.notify("Too early to record another clip");
		} else {
			await this.notify("ERROR: Could not save clip");
		}
	}

	toggleRolling = async (isRolling: boolean) => {
		if (!isRolling) {
			await this.serverAPI.callPluginMethod('enable_rolling', {});
		} else {
			await this.serverAPI.callPluginMethod('disable_rolling', {});
		}
	}
	
	/*
	R2 0
	L2 1
	R1 2
	R2 3
	Y  4
	B  5
	X  6
	A  7
	UP 8
	Right 9
	Left 10
	Down 11
	Select 12
	Steam 13
	Start 14
	QAM  ???
	L5 15
	R5 16
	Share 29 */
	shareButton: HoldDownButton = new HoldDownButton(29, this.saveRollingRecording);
	steamStartComb: CombinedButtons = new CombinedButtons([13, 14], this.saveRollingRecording);
}

const DeckyRecorder: VFC<{ serverAPI: ServerAPI, logic: DeckyRecorderLogic }> = ({ serverAPI, logic }) => {

	const [isCapturing, setCapturing] = useState<boolean>(false);

	// const [mode, setMode] = useState<string>("localFile");

	const [isRolling, setRolling] = useState<boolean>(false);

	const [buttonsEnabled, setButtonsEnabled] = useState<boolean>(true);

	// const audioBitrateOption128 = { data: "128", label: "128 Kbps" } as SingleDropdownOption
	// const audioBitrateOption192 = { data: "192", label: "192 Kbps" } as SingleDropdownOption
	// const audioBitrateOption256 = { data: "256", label: "256 Kbps" } as SingleDropdownOption
	// const audioBitrateOption320 = { data: "320", label: "320 Kbps" } as SingleDropdownOption
	// const audioBitrateOptions: DropdownOption[] = [audioBitrateOption128,
	// 	audioBitrateOption192, audioBitrateOption256, audioBitrateOption320];
	// const [audioBitrate, setAudioBitrate] = useState<DropdownOption>(audioBitrateOption128);

	const [localFilePath, setLocalFilePath] = useState<string>("/home/deck/Videos");

	const formatOptionMp4 = { data: "mp4", label: "MP4" } as SingleDropdownOption
	const formatOptionMkv = { data: "mkv", label: "Matroska (.mkv)" } as SingleDropdownOption;
	const formatOptionMov = { data: "mov", label: "QuickTime (.mov)" } as SingleDropdownOption;
	const formatOptions: DropdownOption[] = [formatOptionMkv, formatOptionMp4, formatOptionMov];
	const [localFileFormat, setLocalFileFormat] = useState<DropdownOption>(formatOptionMp4);

	const initState = async () => {
		const getIsCapturingResponse = await serverAPI.callPluginMethod('is_capturing', {});
		setCapturing(getIsCapturingResponse.result as boolean);

		const getIsRollingResponse = await serverAPI.callPluginMethod('is_rolling', {});
		setRolling(getIsRollingResponse.result as boolean);

		// const getModeResponse = await serverAPI.callPluginMethod('get_current_mode', {});
		// setMode(getModeResponse.result as string);

		// const getAudioBitrateResponse = await serverAPI.callPluginMethod('get_audio_bitrate', {});
		// const audioBitrateResponseNumber: number = getAudioBitrateResponse.result as number;
		// switch (audioBitrateResponseNumber) {
		// 	case 128:
		// 		setAudioBitrate(audioBitrateOption128);
		// 		break;
		// 	case 192:
		// 		setAudioBitrate(audioBitrateOption192)
		// 		break;
		// 	case 256:
		// 		setAudioBitrate(audioBitrateOption256)
		// 		break;
		// 	case 320:
		// 		setAudioBitrate(audioBitrateOption320)
		// 		break;
		// 	default:
		// 		setAudioBitrate(audioBitrateOption128)
		// 		break;
		// }

		const getLocalFilepathResponse = await serverAPI.callPluginMethod('get_local_filepath', {})
		setLocalFilePath(getLocalFilepathResponse.result as string);

		const getLocalFileFormatResponse = await serverAPI.callPluginMethod('get_local_fileformat', {})
		const localFileFormatResponseString: string = getLocalFileFormatResponse.result as string;
		if (localFileFormatResponseString == "mp4") {
			setLocalFileFormat(formatOptionMp4)
		} else if (localFileFormatResponseString == "mkv") {
			setLocalFileFormat(formatOptionMkv)
		} else if (localFileFormatResponseString == "mov") {
			setLocalFileFormat(formatOptionMov)
		} else {
			// should never happen? default back to mp4
			setLocalFileFormat(formatOptionMp4)
		}

	}

	const recordingButtonPress = async () => {
		if (isCapturing === false) {
			setCapturing(true);
			await serverAPI.callPluginMethod('start_capturing', {app_name: Router.MainRunningApp?.display_name});
			Router.CloseSideMenus();
		} else {
			setCapturing(false);
			await serverAPI.callPluginMethod('stop_capturing', {});
		}
	}

	const pickFolder = async () => {
		const filePickerResponse = await serverAPI.openFilePicker(localFilePath, false);
		setLocalFilePath(filePickerResponse.path)
		await serverAPI.callPluginMethod('set_local_filepath', {localFilePath: filePickerResponse.path});
	}

	const rollingRecordButtonPress = async (duration: number) => {
		setButtonsEnabled(false);
		setTimeout(() => {
			setButtonsEnabled(true);
		}, 1000);
		logic.saveRollingRecording(duration);
	}

	const shouldButtonsBeEnabled = () => {
		if (!isCapturing) {
			return false;
		}
		if (!buttonsEnabled) {
			return false;
		}
		return true;
	}

	const disableFileformatDropdown = () => {
		if (isCapturing) {
			return true;
		}
		if (isRolling) {
			return true;
		}
		return false;
	}

	const rollingToggled = async () => {
		logic.toggleRolling(isRolling);
		setCapturing(!isRolling);
		setRolling(!isRolling);
	}

	const getFilePickerText = (): string => {
		return "Recordings will be saved to " + localFilePath;
	}

	const getRecordingButtonText = (): string => {
		if (isCapturing === false) {
			return "Start Recording";
		} else {
			return "Stop Recording";
		}
	}



	useEffect(() => {
		initState();
	}, []);

	return (
		<PanelSection>

			<PanelSectionRow>
				<ToggleField
					label="Replay Mode"
					checked={isRolling}
					onChange={(e) => { setRolling(e); rollingToggled(); }}
				/>
				<div>Steam + Start saves a 30 second clip in replay mode. If replay mode is off, this shortcut will enable it.</div>
				{(!isRolling) ?
					<div>

						<ButtonItem
							bottomSeparator="none"
							layout="below"
							onClick={() => {
								recordingButtonPress();
							}}>
							{getRecordingButtonText()}
						</ButtonItem>

						<ButtonItem
							label={getFilePickerText()}
							bottomSeparator="none"
							layout="below"
							onClick={() => {
								pickFolder();
							}}>
							{"Set folder"}
						</ButtonItem>


					</div> : null
				}
			</PanelSectionRow>

			<PanelSectionRow>
				<Dropdown
					menuLabel="Select the video file format"
					disabled={disableFileformatDropdown()}
					strDefaultLabel={localFileFormat.label as string}
					rgOptions={formatOptions}
					selectedOption={localFileFormat}
					onChange={(newLocalFileFormat) => {
						serverAPI.callPluginMethod('set_local_fileformat', { fileformat: newLocalFileFormat.data });
						setLocalFileFormat(newLocalFileFormat);
					}}
				/>
			</PanelSectionRow>

			{(isRolling)
				? <PanelSectionRow><ButtonItem disabled={!shouldButtonsBeEnabled()} onClick={() => { rollingRecordButtonPress(30) }}>30 sec</ButtonItem></PanelSectionRow> : null}

			{(isRolling)
				? <PanelSectionRow><ButtonItem disabled={!shouldButtonsBeEnabled()} onClick={() => { rollingRecordButtonPress(60) }}>1 min</ButtonItem></PanelSectionRow> : null}

			{(isRolling)
				? <PanelSectionRow><ButtonItem disabled={!shouldButtonsBeEnabled()} onClick={() => { rollingRecordButtonPress(60 * 2) }}>2 min</ButtonItem></PanelSectionRow> : null}

			{(isRolling)
				? <PanelSectionRow><ButtonItem disabled={!shouldButtonsBeEnabled()} onClick={() => { rollingRecordButtonPress(60 * 5) }}>5 min</ButtonItem></PanelSectionRow> : null}

		</PanelSection>
	);

};


export default definePlugin((serverApi: ServerAPI) => {
	let logic = new DeckyRecorderLogic(serverApi);
	let steamStartCombRegister = window.SteamClient.Input.RegisterForControllerStateChanges(logic.steamStartComb.handleButtonInput);
	let holdShareRegister = window.SteamClient.Input.RegisterForControllerStateChanges(logic.shareButton.handleButtonInput);
	//Router.MainRunningApp?.display_name
	return {
		title: <div className={staticClasses.Title}>Decky Recorder</div>,
		content: <DeckyRecorder serverAPI={serverApi} logic={logic} />,
		icon: <FaVideo />,
		onDismount() {
			steamStartCombRegister.unregister();
			holdShareRegister.unregister();
		},
		alwaysRender: true
	};
});