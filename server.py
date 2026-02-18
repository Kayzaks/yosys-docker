import os
import json
import tempfile
import subprocess
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

YOSYS_BIN = os.environ.get("YOSYS_BIN", "yosys")


def generate_liberty(primitives):
    """Generate a minimal Liberty file from the tech library primitives."""
    cells = []
    for prim in primitives:
        name = prim.get("name", "UNKNOWN")
        area = prim.get("area", 1)
        num_inputs = prim.get("numInputs", 2)
        func = prim.get("function", "")

        input_names = [chr(65 + i) for i in range(num_inputs)]  # A, B, C, ...
        output_name = "Y"

        pin_blocks = []
        for pin in input_names:
            pin_blocks.append(f"""
        pin({pin}) {{
            direction : input;
        }}""")

        func_attr = f'\n            function : "{func}";' if func else ""
        pin_blocks.append(f"""
        pin({output_name}) {{
            direction : output;{func_attr}
        }}""")

        cells.append(f"""
    cell({name}) {{
        area : {area};{"".join(pin_blocks)}
    }}""")

    return f"""library(tech) {{{"".join(cells)}
}}
"""


def build_opt_script(abc_settings):
    """Build an ABC optimization-only script (no mapping)."""
    cmds = []

    custom = abc_settings.get("customScript", "")
    # Detect default placeholder
    is_default = not custom or any(
        marker in custom
        for marker in ["# Custom ABC commands", "resyn2\nmap"]
    )
    has_custom = (
        custom
        and not is_default
        and any(
            line.strip() and not line.strip().startswith("#")
            for line in custom.splitlines()
        )
    )

    if has_custom:
        for line in custom.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                # Skip any mapping commands — we handle mapping in step 2
                if stripped in ("map", "amap", "dch", "if"):
                    continue
                # Skip read_liberty/read_lib — handled by Yosys in step 2
                if stripped.startswith("read_lib"):
                    continue
                cmds.append(stripped)
    else:
        opt_cmd = abc_settings.get("optimizationCommand", "resyn2")
        optimize_for = abc_settings.get("optimizeFor", "balanced")

        if optimize_for == "area":
            cmds.append(opt_cmd)
            cmds.append("resyn2")
        elif optimize_for == "delay":
            cmds.append(opt_cmd)
            cmds.append("resyn3")
        else:
            cmds.append(opt_cmd)

    return "\n".join(cmds) if cmds else "resyn2"


def build_yosys_script(
    verilog_path, json_output_path, yosys_settings, liberty_path, abc_opt_script_path
):
    """Build the Yosys synthesis script."""
    synth_cmd = yosys_settings.get("synthCommand", "synth -noabc")
    opt_level = yosys_settings.get("optimizationLevel", "medium")
    flatten = yosys_settings.get("flatten", False)
    custom_script = yosys_settings.get("customScript", "")

    # Map optimization level to Yosys -O flag
    opt_map = {"none": "-O0", "low": "-O1", "medium": "-O2", "high": "-O3"}
    opt_flag = opt_map.get(opt_level, "-O2")

    # Ensure -noabc is present in synth command
    if "synth" in synth_cmd and "-noabc" not in synth_cmd:
        synth_cmd += " -noabc"

    # Check if custom script is just the default placeholder
    is_default_custom = not custom_script or any(
        marker in custom_script
        for marker in ["# Custom Yosys commands", "synth -noabc\nopt\ntechmap\nopt"]
    )

    script = f"read_verilog {verilog_path}\n"

    if not is_default_custom and custom_script.strip():
        # Use custom script but ensure essential commands
        lines = custom_script.strip().splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                if stripped.startswith("read_verilog"):
                    continue  # Already added
                if stripped.startswith("write_json"):
                    continue  # Added at end
                script += f"{stripped}\n"
    else:
        # Standard synthesis flow
        if flatten:
            script += "flatten\n"

        script += f"{synth_cmd} {opt_flag}\n"
        script += "opt\n"
        script += "techmap\n"
        script += "opt\n"

    # Step 1: ABC optimization only (no mapping)
    script += f'abc -script {abc_opt_script_path}\n'

    # Step 2: ABC technology mapping with Liberty
    script += f'abc -liberty {liberty_path}\n'

    # Clean up and output
    script += "opt_clean -purge\n"
    script += f"write_json {json_output_path}\n"

    return script


def parse_yosys_json(json_path):
    """Parse the Yosys JSON output into gates and wires."""
    with open(json_path, "r") as f:
        data = json.load(f)

    gates = []
    wires = []

    modules = data.get("modules", {})
    if not modules:
        return gates, wires

    # Take the first module
    module_name = list(modules.keys())[0]
    module = modules[module_name]

    ports = module.get("ports", {})
    cells = module.get("cells", {})
    netnames = module.get("netnames", {})

    # Build a map from bit number to net name
    bit_to_net = {}
    for net_name, net_info in netnames.items():
        for idx, bit in enumerate(net_info.get("bits", [])):
            if isinstance(bit, int):
                suffix = f"[{idx}]" if len(net_info.get("bits", [])) > 1 else ""
                bit_to_net[bit] = f"{net_name}{suffix}"

    # Create INPUT/OUTPUT gates from ports
    for port_name, port_info in ports.items():
        direction = port_info.get("direction", "")
        bits = port_info.get("bits", [])

        for idx, bit in enumerate(bits):
            if not isinstance(bit, int):
                continue
            suffix = f"[{idx}]" if len(bits) > 1 else ""
            gate_id = f"{port_name}{suffix}"

            if direction == "input":
                gates.append(
                    {
                        "id": gate_id,
                        "type": "INPUT",
                        "inputs": [],
                        "outputs": [str(bit)],
                    }
                )
            elif direction == "output":
                gates.append(
                    {
                        "id": gate_id,
                        "type": "OUTPUT",
                        "inputs": [str(bit)],
                        "outputs": [],
                    }
                )

    # Create gates from cells
    for cell_name, cell_info in cells.items():
        cell_type = cell_info.get("type", "UNKNOWN").lstrip("\\")
        connections = cell_info.get("connections", {})
        port_directions = cell_info.get("port_directions", {})

        input_bits = []
        output_bits = []

        for conn_name, bits in connections.items():
            direction = port_directions.get(conn_name, "input")
            for bit in bits:
                if isinstance(bit, int):
                    if direction == "output":
                        output_bits.append(str(bit))
                    else:
                        input_bits.append(str(bit))

        gates.append(
            {
                "id": cell_name,
                "type": cell_type,
                "inputs": input_bits,
                "outputs": output_bits,
            }
        )

    # Build wires from gate connections
    # Create a map: bit -> producing gate
    bit_producers = {}
    for gate in gates:
        for bit in gate.get("outputs", []):
            bit_producers[bit] = gate["id"]

    for gate in gates:
        for port_idx, bit in enumerate(gate.get("inputs", [])):
            producer = bit_producers.get(bit)
            if producer and producer != gate["id"]:
                wires.append(
                    {
                        "from": producer,
                        "fromPort": f"out_{bit}",
                        "to": gate["id"],
                        "toPort": f"in_{port_idx}",
                    }
                )

    return gates, wires


@app.route("/synthesize", methods=["POST"])
def synthesize():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No JSON body provided"}), 400

        verilog = data.get("verilog", "")
        yosys_settings = data.get("yosys_settings", {})
        abc_settings = data.get("abc_settings", {})
        tech_library = data.get("tech_library", [])

        if not verilog.strip():
            return jsonify({"error": "No Verilog source provided"}), 400

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write Verilog source
            verilog_path = os.path.join(tmpdir, "input.v")
            with open(verilog_path, "w") as f:
                f.write(verilog)

            # Generate Liberty file from tech library
            liberty_path = os.path.join(tmpdir, "tech.lib")
            liberty_content = generate_liberty(tech_library)
            with open(liberty_path, "w") as f:
                f.write(liberty_content)

            # Build ABC optimization script (no mapping commands)
            abc_opt_script_path = os.path.join(tmpdir, "abc_opt.script")
            abc_opt_script = build_opt_script(abc_settings)
            with open(abc_opt_script_path, "w") as f:
                f.write(abc_opt_script)

            # Build Yosys script
            json_output_path = os.path.join(tmpdir, "output.json")
            yosys_script = build_yosys_script(
                verilog_path,
                json_output_path,
                yosys_settings,
                liberty_path,
                abc_opt_script_path,
            )

            script_path = os.path.join(tmpdir, "synth.ys")
            with open(script_path, "w") as f:
                f.write(yosys_script)

            # Run Yosys
            result = subprocess.run(
                [YOSYS_BIN, "-s", script_path],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                return (
                    jsonify(
                        {
                            "error": "Yosys synthesis failed",
                            "stderr": result.stderr[-2000:] if result.stderr else "",
                            "stdout": result.stdout[-2000:] if result.stdout else "",
                        }
                    ),
                    400,
                )

            # Check output exists
            if not os.path.exists(json_output_path):
                return (
                    jsonify(
                        {
                            "error": "Yosys produced no output",
                            "stderr": result.stderr[-2000:] if result.stderr else "",
                            "stdout": result.stdout[-2000:] if result.stdout else "",
                        }
                    ),
                    400,
                )

            # Parse output
            gates, wires = parse_yosys_json(json_output_path)

            # Compute stats
            gate_breakdown = {}
            max_fanout = 0
            for g in gates:
                gtype = g["type"]
                gate_breakdown[gtype] = gate_breakdown.get(gtype, 0) + 1
                fanout = sum(
                    1
                    for w in wires
                    if w["from"] == g["id"]
                )
                max_fanout = max(max_fanout, fanout)

            # Compute max depth via BFS
            adjacency = {}
            in_degree = {}
            for g in gates:
                adjacency.setdefault(g["id"], [])
                in_degree.setdefault(g["id"], 0)
            for w in wires:
                adjacency.setdefault(w["from"], []).append(w["to"])
                in_degree.setdefault(w["to"], 0)
                in_degree[w["to"]] = in_degree.get(w["to"], 0) + 1

            # Topological sort for depth
            depth = {gid: 0 for gid in adjacency}
            queue = [gid for gid, deg in in_degree.items() if deg == 0]
            max_depth = 0
            while queue:
                nxt = []
                for gid in queue:
                    for neighbor in adjacency.get(gid, []):
                        depth[neighbor] = max(depth[neighbor], depth[gid] + 1)
                        max_depth = max(max_depth, depth[neighbor])
                        in_degree[neighbor] -= 1
                        if in_degree[neighbor] == 0:
                            nxt.append(neighbor)
                queue = nxt

            stats = {
                "totalGates": len(gates),
                "gateBreakdown": gate_breakdown,
                "maxDepth": max_depth,
                "maxFanout": max_fanout,
                "wireCount": len(wires),
            }

            return jsonify({"gates": gates, "wires": wires, "stats": stats})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Synthesis timed out (120s limit)"}), 504
    except Exception as e:
        return jsonify({"error": f"Internal error: {str(e)}"}), 500


@app.route("/health", methods=["GET", "HEAD"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
