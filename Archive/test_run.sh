#!/bin/bash
# Auto-test: runs analysis with ShohamDualWirePrint test parameters
# Usage: ./test_run.sh

ZIP="/Users/dannyshoham/Downloads/Dual Material Print - Shoham 2/ShohamDualWirePrint.zip"
API="http://localhost:5050"

echo "🔄 Restarting Flask app with latest code..."
pkill -9 -f "python3 app.py" 2>/dev/null
sleep 2
cd "/Users/dannyshoham/Desktop/Clude code" && nohup python3 app.py > /tmp/meltio_app.log 2>&1 &
sleep 4

echo "🧪 Running test analysis..."
RESULT=$(curl -s -X POST "$API/api/analyze" \
  -F "zip_file=@$ZIP" \
  -F "part_name=ShohamDualWirePrint" \
  -F "material_T0=316L" \
  -F "material_T1=316L" \
  -F "laser_power=1400" \
  -F "feed_speed=17" \
  -F "layer_height=1.2" \
  -F "layer_width=1" \
  -F "wire_diameter=1")

HTML_3D=$(echo "$RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('viz',{}).get('3d') or [v for v in d.values() if isinstance(v,str) and '3d' in v][0])" 2>/dev/null)

if [ -z "$HTML_3D" ]; then
  echo "❌ Analysis failed. Check /tmp/meltio_app.log"
  exit 1
fi

echo "✅ Analysis complete"
echo "   3D heatmap: $HTML_3D"

# Sync outputs to preview server
cp -r "/Users/dannyshoham/Desktop/Clude code/outputs/." /tmp/meltio_server/outputs/ 2>/dev/null

# Get just the filename for the preview URL
FILENAME=$(basename "$HTML_3D")
echo "   Preview:    http://localhost:59458/outputs/$FILENAME"
open "http://localhost:59458/outputs/$FILENAME" 2>/dev/null || true
