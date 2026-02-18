from flask import Flask, request, jsonify
from flask_cors import CORS
import subprocess, json, tempfile, os

app = Flask(__name__)
CORS(app)

@app.route("/", methods=["HEAD", "GET"])
def health():
    return "OK"

@app.route("/synthesize", methods=["POST"])
def synthesize():
    data = request.json
    verilog = data.get("verilog", "")
    yosys_settings = data.get("yosys_settings", {})

    with tempfile.TemporaryDirectory() as tmpdir:
        verilog_file = os.path.join(tmpdir, "input.v")
        out_json = os.path.join(tmpdir, "out.json")
        script_file = os.path.join(tmpdir, "synth.ys")

        with open(verilog_file, "w") as f:
            f.write(verilog)

        # Build Yosys script
        flatten = " -flatten" if yosys_settings.get("flatten") else ""
        script = f"read_verilog {verilog_file}\nsynth{flatten}\nwrite_json {out_json}\n"

        with open(script_file, "w") as f:
            f.write(script)

        # Run Yosys and CHECK the result
        result = subprocess.run(
            ["yosys", "-s", script_file],
            capture_output=True, text=True
        )

        if result.returncode != 0:
            return jsonify({
                "error": "Yosys synthesis failed",
                "stderr": result.stderr[-2000:],  # last 2000 chars
                "stdout": result.stdout[-2000:]
            }), 400

        if not os.path.exists(out_json):
            return jsonify({
                "error": "Yosys did not produce output",
                "stderr": result.stderr[-2000:]
            }), 400

        with open(out_json) as f:
            yosys_json = json.load(f)

        netlist = transform_netlist(yosys_json)
        return jsonify(netlist)


def transform_netlist(yosys_json):
    gates = []
    wires = []
    net_producer = {}  # net_id -> gate_id

    for mod_name, mod in yosys_json.get("modules", {}).items():
        # Ports → INPUT/OUTPUT gates
        for port_name, port in mod.get("ports", {}).items():
            direction = port["direction"]
            gate_type = "INPUT" if direction == "input" else "OUTPUT"
            for i, bit in enumerate(port["bits"]):
                gid = f"port_{port_name}_{i}"
                net = f"n{bit}"
                if gate_type == "INPUT":
                    gates.append({"id": gid, "type": "INPUT", "inputs": [], "outputs": [net], "properties": {"name": f"{port_name}[{i}]" if len(port["bits"]) > 1 else port_name}})
                    net_producer[net] = gid
                else:
                    gates.append({"id": gid, "type": "OUTPUT", "inputs": [net], "outputs": [], "properties": {"name": f"{port_name}[{i}]" if len(port["bits"]) > 1 else port_name}})

        # Cells → logic gates
        for cell_name, cell in mod.get("cells", {}).items():
            gid = f"cell_{cell_name}"
            cell_type = cell["type"].lstrip("$_").rstrip("_")
            inp_nets = []
            out_nets = []

            for conn_name, bits in cell.get("connections", {}).items():
                direction = cell.get("port_directions", {}).get(conn_name, "input")
                for bit in bits:
                    net = f"n{bit}"
                    if direction == "output":
                        out_nets.append(net)
                        net_producer[net] = gid
                    else:
                        inp_nets.append(net)

            gates.append({"id": gid, "type": cell_type, "inputs": inp_nets, "outputs": out_nets, "properties": {"name": cell_name}})

    # Build wires
    for g in gates:
        for inp in g["inputs"]:
            src = net_producer.get(inp)
            if src:
                wires.append({"from": src, "fromPort": inp, "to": g["id"], "toPort": inp})

    return gates, wires


def compute_stats(gates, wires):
    breakdown = {}
    total = 0
    for g in gates:
        if g["type"] not in ("INPUT", "OUTPUT"):
            breakdown[g["type"]] = breakdown.get(g["type"], 0) + 1
            total += 1

    # Fan-out
    net_fanout = {}
    for g in gates:
        for o in g["outputs"]:
            net_fanout[o] = 0
    for g in gates:
        for i in g["inputs"]:
            net_fanout[i] = net_fanout.get(i, 0) + 1
    max_fanout = max(net_fanout.values()) if net_fanout else 0

    # Combinational depth
    is_ff = {g["id"] for g in gates if g["type"].startswith("DFF") or g["type"].startswith("DFFE") or "FF" in g["type"]}
    is_io = {g["id"] for g in gates if g["type"] in ("INPUT", "OUTPUT")}
    net_prod = {}
    for g in gates:
        for o in g["outputs"]:
            net_prod[o] = g["id"]

    combo_preds = {}
    for g in gates:
        if g["id"] in is_ff or g["id"] in is_io:
            continue
        preds = []
        for i in g["inputs"]:
            p = net_prod.get(i)
            if p and p not in is_ff and p not in is_io:
                preds.append(p)
        combo_preds[g["id"]] = preds

    cache = {}
    def depth(gid):
        if gid in cache:
            return cache[gid]
        ps = combo_preds.get(gid, [])
        d = 1 if not ps else max(depth(p) for p in ps) + 1
        cache[gid] = d
        return d

    max_depth = 0
    for gid in combo_preds:
        max_depth = max(max_depth, depth(gid))

    return {
        "totalGates": total,
        "gateBreakdown": breakdown,
        "maxDepth": max_depth,
        "maxFanout": max_fanout,
        "wireCount": len(wires),
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)