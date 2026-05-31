# Meltio DED Analysis Agent - Quick Start

## What It Does
Analyzes RAPID code from Meltio DED metal printing jobs and generates detailed reports.

## Quick Start

### 1. Prepare Your ZIP File
Bundle all `.mod` files (one per layer) into a ZIP archive:
```
your_print_job.zip
├── layer_1.mod
├── layer_2.mod
├── layer_3.mod
└── ...
```

### 2. Run the Agent
```bash
python meltio_ded_analyzer.py your_print_job.zip
```

### 3. Answer Questions
The agent will ask:
```
📝 Part name [your_print_job]: [your input]
🔍 Found 2 materials in code: T0, T1
   Material T0 type: [your input]
   Material T1 type: [your input]
⚡ Laser power (W): [your input]
🔄 Wire feed speed (mm/sec): [your input]
```

### 4. Get Your Report
Report automatically saved to:
```
outputs/meltio-analysis_[part_name]_[timestamp].md
```

## Files Created
- `meltio_ded_analyzer.py` - Main agent script
- `workflow/meltio-ded-analysis.md` - Detailed workflow documentation
- `outputs/meltio-analysis_*.md` - Generated reports

## What the Agent Analyzes

### From RAPID Code:
- ✅ Number of layers
- ✅ Robot speeds (print vs travel)
- ✅ Material feeders (T0, T1)
- ✅ Digital I/O configuration
- ✅ G-code commands
- ✅ Print sequence operations

### From User Input:
- ✅ Part name
- ✅ Material types
- ✅ Laser power
- ✅ Wire feed speed

## Report Example
```
# Meltio DED Print Analysis Report
**Part Name:** component_001
**Total Layers:** 45
**Materials:** T0=Titanium, T1=Nickel
**Laser Power:** 500 W
**Wire Feed Speed:** 8 mm/sec
...
```

## Next Phase Features (Coming Soon)
- Advanced parameter validation
- Safety checks
- Performance optimization recommendations
- Layer-by-layer detailed analysis
- Comparison with previous prints

## Troubleshooting

**Error: Invalid ZIP file**
- Ensure your file is a valid ZIP archive
- Verify file extension is `.zip`

**Error: No .mod files found**
- Check ZIP contains `.mod` files
- Verify correct file names

**Issues with parsing?**
- Check RAPID syntax in .mod files
- Ensure UTF-8 encoding

## Support
See `workflow/meltio-ded-analysis.md` for detailed documentation.
