import json
import os
import subprocess
import tempfile
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


def generate_liberty(tech_library, path):
    """Generate a minimal Liberty .lib file from the user's tech library."""
    lines = ['library(tech) {']

    # Always include an inverter so ABC can map
    has_inv = any(
        p.get('function') and p.get('numInputs', 0) == 1
        and "'" in p.get('function', '')
        for p in tech_library
    )
    if not has_inv:
        lines.append('  cell(_auto_inv_) {')
        lines.append('    area : 0.5;')
        lines.append('    pin(A) {')
        lines.append('      direction : input;')
        lines.append('    }')
        lines.append('    pin(Y) {')
        lines.append('      direction : output;')
        lines.append('      function : "A\'";')
        lines.append('    }')
        lines.append('  }')

    for prim in tech_library:
        name = prim['name']
        area = prim.get('area', 1.0)
        num_inputs = prim.get('numInputs', 2)
        func = prim.get('function', '')

        lines.append(f'  cell({name}) {{')
        lines.append(f'    area : {area};')

        pin_names = [chr(ord('A') + i) for i in range(num_inputs)]
        for pin in pin_names:
            lines.append(f'    pin({pin}) {{')
            lines.append(f'      direction : input;')
            lines.append(f'    }}')

        lines.append(f'    pin(Y) {{')
        lines.append(f'      direction : output;')
        if func:
            lines.append(f'      function : "{func}";')
        lines.append(f'    }}')

        lines.append('  }')
    lines.append('}')
    with open(path, 'w') as f:
        f.write('\n'.join(lines))



def transform_netlist(yosys_json):
    """Convert Yosys JSON output to [gates, wires] format."""
    gates = []
    wires = []
    net_drivers = {}
    net_consumers = {}

    modules = yosys_json.get("modules", {})
    if not modules:
        return [[], []]

    module_name = list(modules.keys())[0]
    module = modules[module_name]

    ports = module.get("ports", {})
    for port_name, port_info in ports.items():
        direction = port_info.get("direction", "input")
        bits = port_info.get("bits", [])

        for idx, bit in enumerate(bits):
            if isinstance(bit, str):
                continue
            suffix = f"_{idx}" if len(bits) > 1 else "_0"
            gate_id = f"port_{port_name}{suffix}"
            net_name = f"n{bit}"

            if direction == "input":
                gates.append({
                    "id": gate_id,
                    "type": "INPUT",
                    "inputs": [],
                    "outputs": [net_name],
                    "properties": {"name": port_name if len(bits) == 1 else f"{port_name}[{idx}]"}
                })
                net_drivers[net_name] = gate_id
            else:
                gates.append({
                    "id": gate_id,
                    "type": "OUTPUT",
                    "inputs": [net_name],
                    "outputs": [],
                    "properties": {"name": port_name if len(bits) == 1 else f"{port_name}[{idx}]"}
                })
                if net_name not in net_consumers:
                    net_consumers[net_name] = []
                net_consumers[net_name].append((gate_id, net_name))

    cells = module.get("cells", {})
    for cell_name, cell_info in cells.items():
        cell_type = cell_info.get("type", "UNKNOWN")
        if cell_type.startswith("$_"):
            cell_type = cell_type[2:]
        if cell_type.endswith("_"):
            cell_type = cell_type[:-1]

        connections = cell_info.get("connections", {})
        port_directions = cell_info.get("port_directions", {})

        input_nets = []
        output_nets = []
        gate_id = f"cell_{cell_name}"

        for conn_name, bits in connections.items():
            direction = port_directions.get(conn_name, "input")
            for bit in bits:
                if isinstance(bit, str):
                    continue
                net_name = f"n{bit}"
                if direction == "output":
                    output_nets.append(net_name)
                    net_drivers[net_name] = gate_id
                else:
                    input_nets.append(net_name)
                    if net_name not in net_consumers:
                        net_consumers[net_name] = []
                    net_consumers[net_name].append((gate_id, net_name))

        gates.append({
            "id": gate_id,
            "type": cell_type,
            "inputs": input_nets,
            "outputs": output_nets,
            "properties": {"name": cell_name}
        })

    for net_name, driver_id in net_drivers.items():
        consumers = net_consumers.get(net_name, [])
        for consumer_id, consumer_port in consumers:
            wires.append({
                "from": driver_id,
                "fromPort": net_name,
                "to": consumer_id,
                "toPort": consumer_port
            })

    return [gates, wires]


@app.route("/", methods=["HEAD", "GET"])
def health():
    return "OK", 200


@app.route("/synthesize", methods=["POST"])
def synthesize():
    data = request.json
    verilog = data.get("verilog", "")
    yosys_settings = data.get("yosys_settings", {})
    tech_library = data.get("tech_library", [])

    with tempfile.TemporaryDirectory() as tmpdir:
        verilog_file = os.path.join(tmpdir, "input.v")
        out_json = os.path.join(tmpdir, "out.json")
        script_file = os.path.join(tmpdir, "synth.ys")

        with open(verilog_file, "w") as f:
            f.write(verilog)

        flatten = " -flatten" if yosys_settings.get("flatten") else ""

        if tech_library:
            lib_file = os.path.join(tmpdir, "tech.lib")
            generate_liberty(tech_library, lib_file)
            has_ff = any(p['type'].upper().startswith('DFF') for p in tech_library)
            script = (
                f"read_verilog {verilog_file}\n"
                f"synth{flatten}\n"
            )
            if has_ff:
                script += f"dfflibmap -liberty {lib_file}\n"
            script += (
                f"abc -liberty {lib_file}\n"
                f"write_json {out_json}\n"
            )
        else:
            script = (
                f"read_verilog {verilog_file}\n"
                f"synth{flatten}\n"
                f"write_json {out_json}\n"
            )

        with open(script_file, "w") as f:
            f.write(script)

        result = subprocess.run(
            ["yosys", "-s", script_file],
            capture_output=True, text=True
        )

        if result.returncode != 0:
            return jsonify({
                "error": "Yosys synthesis failed",
                "stderr": result.stderr[-2000:],
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
