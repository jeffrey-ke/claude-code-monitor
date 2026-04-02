for f in ~/.claude/sessions/*.json; do
	pid=$(basename $f .json)
	if [ -d "/proc/$pid" ]; then
		echo "ALIVE  $pid: $(cat $f | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("name",""), d.get("cwd",""))')"
	else
		echo "DEAD   $pid"
	fi
done
