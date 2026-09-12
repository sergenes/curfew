# CAX80 SOAP capability matrix

Endpoint: `https://192.168.1.1:5043/soap/server_sa/`  
Probed: 2026-09-12 13:37:13  
Login: SOAP v2 (`DeviceConfig:1#SOAPLogin`, session cookie)

Legend: `0000` supported, `501` action not found, `404` service not found, `402` missing parameter, `401` unauthorized.

| Service | Action | Code | ms | Fields |
|---|---|---|---|---|
| DeviceInfo:1 | GetInfo | 000 | 131 | ModelName, Description, SerialNumber, Firmwareversion, SmartAgentversion, FirewallVersion, VPNVersion, OthersoftwareVersion, Hardwareversion, Otherhardwareversion, FirstUseDate, DeviceName, FirmwareDLmethod, FirmwareLastUpdate, FirmwareLastChecked, DeviceMode |
| DeviceInfo:1 | GetSysUpTime | 000 | 111 | SysUpTime |
| DeviceInfo:1 | GetSystemInfo | 000 | 99 | NewCPUUtilization, NewPhysicalMemory, NewMemoryUtilization, NewPhysicalFlash, NewAvailableFlash |
| DeviceInfo:1 | GetSupportFeatureListXML | 000 | 116 | newFeatureList |
| DeviceInfo:1 | GetAttachDevice | 000 | 29939 | NewAttachDevice |
| DeviceInfo:1 | GetAttachDevice2 | 000 | 1712 | NewAttachDevice |
| DeviceInfo:1 | GetDeviceListAll | 501 | 92 |  |
| DeviceInfo:1 | GetAllSatellites | 501 | 183 |  |
| DeviceInfo:1 | GetSystemLogs | 000 | 219 | NewLogDetails |
| DeviceConfig:1 | GetInfo | 000 | 135 | BlankState, NewBlockSiteEnable, NewBlockSiteName, NewTimeZone, NewDaylightSaving, TimeZoneOffset, TimeZoneState |
| DeviceConfig:1 | GetTimeZoneInfo | 000 | 115 | NewTimeZone, NewDaylightSaving |
| DeviceConfig:1 | GetTrafficMeterEnabled | 000 | 112 | NewTrafficMeterEnable |
| DeviceConfig:1 | GetTrafficMeterOptions | 000 | 263 | NewControlOption, NewMonthlyLimit, RestartHour, RestartMinute, RestartDay |
| DeviceConfig:1 | GetTrafficMeterStatistics | 000 | 177 | NewTodayConnectionTime, NewTodayUpload, NewTodayDownload, NewYesterdayConnectionTime, NewYesterdayUpload, NewYesterdayDownload, NewWeekConnectionTime, NewWeekUpload, NewWeekDownload, NewMonthConnectionTime, NewMonthUpload, NewMonthDownload, NewLastMonthConnectionTime, NewLastMonthUpload, NewLastMonthDownload |
| DeviceConfig:1 | GetBlockDeviceEnableStatus | 000 | 64 | NewBlockDeviceEnable |
| DeviceConfig:1 | GetBlockSiteInfo | 000 | 66 | NewBlockSiteEnable, NewBlockSiteName |
| DeviceConfig:1 | GetQoSEnableStatus | 000 | 86 |  |
| DeviceConfig:1 | CheckNewFirmware | 000 | 8496 | CurrentVersion, NewVersion, ReleaseNote |
| DeviceConfig:1 | GetDeviceConfig | 501 | 151 |  |
| LANConfigSecurity:1 | GetInfo | 000 | 92 | NewLANSubnet, NewWANLAN_Subnet_Match, NewLANMACAddress, NewLANIP, NewDHCPEnabled |
| WANIPConnection:1 | GetInfo | 000 | 123 | NewEnable, NewConnectionType, NewExternalIPAddress, NewSubnetMask, NewAddressingType, NewDefaultGateway, NewMACAddress, NewMACAddressOverride, NewMaxMTUSize, NewDNSEnabled, NewDNSServers |
| WANIPConnection:1 | GetConnectionTypeInfo | 000 | 105 | NewConnectionType |
| WANIPConnection:1 | GetPortMappingInfo | 000 | 231 | NewPortMappingNumberOfEntries, NewPortMappingInfo |
| WANEthernetLinkConfig:1 | GetEthernetLinkStatus | 000 | 128 | NewEthernetLinkStatus |
| ParentalControl:1 | GetEnableStatus | 000 | 127 | ParentalControl |
| ParentalControl:1 | GetAllMACAddresses | 000 | 100 | AllMACAddresses |
| ParentalControl:1 | GetDNSMasqDeviceID | 001 | 77 |  |
| AdvancedQoS:1 | GetQoSEnableStatus | 501 | 298 |  |
| AdvancedQoS:1 | GetBandwidthControlOptions | 501 | 79 |  |
| AdvancedQoS:1 | GetOOKLASpeedTestResult | 501 | 243 |  |
| AdvancedQoS:1 | GetCurrentDeviceBandwidth | 501 | 67 |  |
| AdvancedQoS:1 | GetCurrentAppBandwidth | 501 | 103 |  |
| WLANConfiguration:1 | GetInfo | 000 | 71 | NewEnable, NewSSIDBroadcast, NewStatus, NewSSID, NewRegion, NewChannel, NewWirelessMode, NewBasicEncryptionModes, NewWEPAuthType, NewWPAEncryptionModes, NewWLANMACAddress |
| WLANConfiguration:1 | Get5GInfo | 000 | 115 | NewEnable, NewSSIDBroadcast, NewStatus, NewSSID, NewRegion, NewChannel, NewWirelessMode, NewBasicEncryptionModes, NewWEPAuthType, NewWPAEncryptionModes, NewWLANMACAddress |
| WLANConfiguration:1 | GetChannelInfo | 000 | 206 | NewChannel |
| WLANConfiguration:1 | Get5GChannelInfo | 000 | 83 | New5GChannel |
| WLANConfiguration:1 | GetAvailableChannel | 402 | 130 |  |
| WLANConfiguration:1 | GetRegion | 000 | 96 | NewRegion |
| WLANConfiguration:1 | GetWPASecurityKeys | 000 | 178 | NewWPAPassphrase |
| WLANConfiguration:1 | Get5GWPASecurityKeys | 000 | 152 | NewWPAPassphrase |
| WLANConfiguration:1 | GetGuestAccessEnabled | 000 | 896 | NewGuestAccessEnabled |
| WLANConfiguration:1 | GetGuestAccessEnabled2 | 501 | 131 |  |
| WLANConfiguration:1 | Get5GGuestAccessEnabled | 000 | 90 | NewGuestAccessEnabled |
| WLANConfiguration:1 | Get5G1GuestAccessEnabled | 501 | 79 |  |
| WLANConfiguration:1 | Get5GGuestAccessEnabled2 | 501 | 86 |  |
| WLANConfiguration:1 | GetGuestAccessNetworkInfo | 000 | 150 | NewSSID, NewSecurityMode, NewKey, UserSetSchedule, Schedule |
| WLANConfiguration:1 | Get5GGuestAccessNetworkInfo | 000 | 68 | NewSSID, NewSecurityMode, NewKey, UserSetSchedule, Schedule |
| WLANConfiguration:1 | IsSmartConnectEnabled | 000 | 99 | NewSmartConnectEnable |
| WLANConfiguration:1 | GetWLANMACAddress | 501 | 80 |  |

## Supported action details

### DeviceInfo:1 GetInfo

- `ModelName`: CAX80
- `Description`: DOCSIS 3.1 Cable Modem Gateway Device
- `SerialNumber`: 60R***
- `Firmwareversion`: V5.1.1.8
- `SmartAgentversion`: N/A
- `FirewallVersion`: N/A
- `VPNVersion`: N/A
- `OthersoftwareVersion`: N/A
- `Hardwareversion`: 1.01
- `Otherhardwareversion`: N/A
- `FirstUseDate`: N/A
- `DeviceName`: CAX80
- `FirmwareDLmethod`: Null
- `FirmwareLastUpdate`: Null
- `FirmwareLastChecked`: Null
- `DeviceMode`: 0

### DeviceInfo:1 GetSysUpTime

- `SysUpTime`: 4 days 14:57:40

### DeviceInfo:1 GetSystemInfo

- `NewCPUUtilization`: 227
- `NewPhysicalMemory`: 768
- `NewMemoryUtilization`: 50
- `NewPhysicalFlash`: 512
- `NewAvailableFlash`: 256

### DeviceInfo:1 GetSupportFeatureListXML

- `newFeatureList`: <1 child nodes>

### DeviceInfo:1 GetAttachDevice

- `NewAttachDevice`: 23@1;192.168.1.202;Unknown;98:DA:C4:**:**:**;wireless;100;100;Allow@2;192.168.1.51;HS200;98:DA:C4:**:**:**;wireless;1...

### DeviceInfo:1 GetAttachDevice2

- `NewAttachDevice`: <25 child nodes>

### DeviceInfo:1 GetSystemLogs

- `NewLogDetails`: [DHCP IP: 192.168.1.186] to MAC address f0:03:8c:**:**:**, Sat Sep 12 12:36:15 2026
[Access Control] Device Unknown w...

### DeviceConfig:1 GetInfo

- `BlankState`: 0
- `NewBlockSiteEnable`: 0
- `NewBlockSiteName`: 0
- `NewTimeZone`: -5
- `NewDaylightSaving`: 0
- `TimeZoneOffset`: -300
- `TimeZoneState`: 0

### DeviceConfig:1 GetTimeZoneInfo

- `NewTimeZone`: -5
- `NewDaylightSaving`: 0

### DeviceConfig:1 GetTrafficMeterEnabled

- `NewTrafficMeterEnable`: 0

### DeviceConfig:1 GetTrafficMeterOptions

- `NewControlOption`: No Limit
- `NewMonthlyLimit`: 0
- `RestartHour`: 0
- `RestartMinute`: 0
- `RestartDay`: 1

### DeviceConfig:1 GetTrafficMeterStatistics

- `NewTodayConnectionTime`: 00:00
- `NewTodayUpload`: 0.00
- `NewTodayDownload`: 0.00
- `NewYesterdayConnectionTime`: 00:00
- `NewYesterdayUpload`: 0.00
- `NewYesterdayDownload`: 0.00
- `NewWeekConnectionTime`: 00:00
- `NewWeekUpload`: 0.00/0.00
- `NewWeekDownload`: 0.00/0.00
- `NewMonthConnectionTime`: 00:00
- `NewMonthUpload`: 0.00/0.00
- `NewMonthDownload`: 0.00/0.00
- `NewLastMonthConnectionTime`: 00:00
- `NewLastMonthUpload`: 0.00/0.00
- `NewLastMonthDownload`: 0.00/0.00

### DeviceConfig:1 GetBlockDeviceEnableStatus

- `NewBlockDeviceEnable`: 0

### DeviceConfig:1 GetBlockSiteInfo

- `NewBlockSiteEnable`: 0
- `NewBlockSiteName`: 0

### DeviceConfig:1 CheckNewFirmware

- `CurrentVersion`: 5.1.***.***
- `NewVersion`: 
- `ReleaseNote`: 

### LANConfigSecurity:1 GetInfo

- `NewLANSubnet`: 255.255.***.***
- `NewWANLAN_Subnet_Match`: 0
- `NewLANMACAddress`: 941865******
- `NewLANIP`: 192.168.1.1
- `NewDHCPEnabled`: true

### WANIPConnection:1 GetInfo

- `NewEnable`: 1
- `NewConnectionType`: DHCP
- `NewExternalIPAddress`: 209.6.***.***
- `NewSubnetMask`: 255.255.***.***
- `NewAddressingType`: DHCP
- `NewDefaultGateway`: 209.6.***.***
- `NewMACAddress`: 941865******
- `NewMACAddressOverride`: 0
- `NewMaxMTUSize`: 1500
- `NewDNSEnabled`: 1
- `NewDNSServers`: 208.59.***.*** 208.59.***.***

### WANIPConnection:1 GetConnectionTypeInfo

- `NewConnectionType`: DHCP

### WANIPConnection:1 GetPortMappingInfo

- `NewPortMappingNumberOfEntries`: 0
- `NewPortMappingInfo`: 

### WANEthernetLinkConfig:1 GetEthernetLinkStatus

- `NewEthernetLinkStatus`: Up

### ParentalControl:1 GetEnableStatus

- `ParentalControl`: 0

### ParentalControl:1 GetAllMACAddresses

- `AllMACAddresses`: 

### WLANConfiguration:1 GetInfo

- `NewEnable`: 1
- `NewSSIDBroadcast`: 1
- `NewStatus`: Up
- `NewSSID`: ***
- `NewRegion`: US
- `NewChannel`: Auto
- `NewWirelessMode`: 1200Mbps
- `NewBasicEncryptionModes`: WPA2-Personal
- `NewWEPAuthType`: Automatic
- `NewWPAEncryptionModes`: WPA2-Personal
- `NewWLANMACAddress`: 941865******

### WLANConfiguration:1 Get5GInfo

- `NewEnable`: 1
- `NewSSIDBroadcast`: 1
- `NewStatus`: Up
- `NewSSID`: ***
- `NewRegion`: US
- `NewChannel`: 44
- `NewWirelessMode`: 4800Mbps
- `NewBasicEncryptionModes`: WPA2-Personal
- `NewWEPAuthType`: Open
- `NewWPAEncryptionModes`: WPA2-Personal
- `NewWLANMACAddress`: 941865******

### WLANConfiguration:1 GetChannelInfo

- `NewChannel`: Auto

### WLANConfiguration:1 Get5GChannelInfo

- `New5GChannel`: 44

### WLANConfiguration:1 GetRegion

- `NewRegion`: US

### WLANConfiguration:1 GetWPASecurityKeys

- `NewWPAPassphrase`: <redacted>

### WLANConfiguration:1 Get5GWPASecurityKeys

- `NewWPAPassphrase`: <redacted>

### WLANConfiguration:1 GetGuestAccessEnabled

- `NewGuestAccessEnabled`: 0

### WLANConfiguration:1 Get5GGuestAccessEnabled

- `NewGuestAccessEnabled`: 0

### WLANConfiguration:1 GetGuestAccessNetworkInfo

- `NewSSID`: ***
- `NewSecurityMode`: WPA2-Personal
- `NewKey`: <redacted>
- `UserSetSchedule`: 0
- `Schedule`: 0

### WLANConfiguration:1 Get5GGuestAccessNetworkInfo

- `NewSSID`: ***
- `NewSecurityMode`: WPA2-Personal
- `NewKey`: <redacted>
- `UserSetSchedule`: 0
- `Schedule`: 0

### WLANConfiguration:1 IsSmartConnectEnabled

- `NewSmartConnectEnable`: 0

