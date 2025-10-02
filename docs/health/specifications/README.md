# GPS Receiver Technical Specifications

This directory contains technical documentation, manuals, and API references for supported GPS receiver types.

## Directory Structure

```
specifications/
├── README.md                          # This file
├── septentrio/                        # Septentrio PolaRX5 documentation
│   ├── PolaRX5_ReferenceManual.pdf   # (to be added)
│   ├── SBF_ReferenceGuide.pdf        # (to be added)
│   └── RxTools_UserManual.pdf        # (to be added)
├── trimble/                           # Trimble receiver documentation
│   ├── NetR9_UserGuide_v4_15.pdf     # (to be added)
│   └── NetRS_Documentation.pdf       # (to be added)
├── leica/                             # Leica receiver documentation
│   └── GR10_UserManual.pdf           # (to be added)
└── links.md                           # Online resource links
```

## Online Resources

### Septentrio PolaRX5
- **Product Page**: https://www.septentrio.com/en/products/gnss-receivers/gnss-reference-receivers/polarx-5
- **Resources**: https://www.septentrio.com/en/products/gnss-receivers/gnss-reference-receivers/polarx-5#resources
- **RxTools**: https://www.septentrio.com/en/products/gps-gnss-receiver-software/rxtools#resources
- **SBF Format**: Septentrio Binary Format reference guide (download from resources page)

### Trimble NetR9
- **User Guide**: https://epic.awi.de/id/eprint/52580/1/Trimble_NetR9_UserGuide_V4_15_RevA_2010.pdf
- **UNAVCO KB**: https://kb.unavco.org/category/gnss-and-related-equipment/gnss-receivers/trimble/trimble-netr9/191/

### Trimble NetRS
- **UNAVCO KB**: https://kb.unavco.org/article/trimble-netrs-resource-page-471.html

### Leica GR10/G10
- **UNAVCO KB**: https://kb.unavco.org/article/137/leica-gr10-resource-page-674.html

### General GNSS Resources
- **UNAVCO GNSS Equipment**: https://kb.unavco.org/category/gnss-and-related-equipment/2/
- **IGS Resources**: http://www.igs.org/

## Document Collection Process

### To Add Documentation:
1. Download manuals from manufacturer websites
2. Rename using consistent naming convention:
   - `<Manufacturer>_<Model>_<DocumentType>_<Version>.pdf`
   - Example: `Septentrio_PolaRX5_ReferenceManual_v5.4.pdf`
3. Place in appropriate subdirectory (septentrio/, trimble/, leica/)
4. Update this README with file details
5. Extract relevant sections for health monitoring into parent documentation

### Copyright Notice
All manufacturer documentation is copyright of respective manufacturers. These files are maintained for internal reference and technical support purposes only. Please refer to manufacturer websites for latest versions and licensing information.

## Key Information Extracted

### PolaRX5 Health Messages (SBF Format)

| Message ID | Name | Description | Health Metrics |
|------------|------|-------------|----------------|
| 4101 | PowerStatus | Power supply information | Voltage, power source, battery |
| 4059 | DiskStatus | Internal storage status | Free space, usage % |
| 4014 | ReceiverStatus | Overall receiver status | CPU load, uptime, error codes |
| 4054 | WiFiAPStatus | WiFi access point status | Connected clients, signal |
| 4102 | LogStatus | Logging session status | Active sessions, errors |
| 4122 | NTRIPServerStatus | NTRIP server status | Client connections |
| 4053 | NTRIPClientStatus | NTRIP client status | Connection, corrections age |

### Trimble NetR9 HTTP Endpoints

| Endpoint | Method | Description | Response Format |
|----------|--------|-------------|-----------------|
| /status | GET | Overall receiver status | HTML |
| /voltage | GET | Power supply voltage | HTML |
| /temperature | GET | Internal temperature | HTML |
| /tracking | GET | Satellite tracking | HTML |
| /logging | GET | Logging status | HTML |
| /sessions | GET | Session information | HTML |

### Leica G10 Limitations

- No direct health API available
- Limited to FTP connection testing
- Health inferred from data flow and file timestamps
- Requires further research for enhanced monitoring

## Maintenance

**Last Updated**: 2025-10-02
**Status**: Initial structure created, documents to be added
**Maintainer**: Veðurstofa Íslands GPS Team

### TODO
- [ ] Download and add PolaRX5 reference manual
- [ ] Download and add SBF reference guide
- [ ] Download and add RxTools documentation
- [ ] Download and add Trimble NetR9 user guide
- [ ] Research and document Leica G10 health capabilities
- [ ] Extract health-specific sections from manuals
- [ ] Create quick reference guides for each receiver type
