from flask import Flask, request, jsonify
from flask_cors import CORS
import tempfile
import os
import subprocess
import json

app = Flask(__name__)
CORS(app)


def generate_liberty(primitives):
    cells = ""
    for p in primitives:
        func = p.get("function", "")
        num_inputs = p.get("numInputs", 2)
        area = p.get("area", 1)
        name = p.get("name", p.get("type", "GATE"))

        pins = ""
        for i in range(num_inputs):
            pin_name = chr(65 + i)
            pins += f"""
        pin({pin_name}) {{
            direction : input ;
            capacitance : 1.0 ;
        }}"""

        pins += f"""
        pin(Y) {{
            direction : output ;
            capacitance : 0.0 ;
            function : "{func}" ;
        }}"""

        cells += f"""
    cell({name}) {{
        area : {area} ;{pins}
    }}"""

    return f"""library(tech) {{
    delay_model : table_lookup ;
    time_unit : "1ns" ;
    capacitive_load_unit(1, pf) ;{cells}
}}
"""


def parse_yosys_json(json_path):
    with open(json_path, "r") as f:
        data = json.load(f)

    gates = []
    wires = []

    for mod_name, mod in data.get("modules", {}).items():
        # Collect port info for INPUT/OUTPUT nodes
        for port_name, port in mod.get("ports", {}).items():
            direction = port.get("direction", "")
            gate_type = "INPUT" if direction == "input" else "OUTPUT"
            for i, bit in enumerate(port.get("bits", [])):
                gate_id = f"{gate_type}_{port_name}_{i}"
                gates.append({
                    "id": gate_id,
                    "type": gate_type,
                    "inputs": [str(bit)] if gate_type == "OUTPUT" else [],
                    "outputs": [str(bit)] if gate_type == "INPUT" else [],
                    "properties": {"name": f"{port_name}[{i}]" if len(port.get("bits", [])) > 1 else port_name}
                })

        # Collect cells
        for cell_name, cell in mod.get("cells", {}).items():
            cell_type = cell.get("type", "BUF").lstrip("$_").rstrip("_")
            connections = cell.get("connections", {})

            input_bits = []
            output_bits = []
            for pin_name, bits in connections.items():
                pin_dir = cell.get("port_directions", {}).get(pin_name, "input")
                for bit in bits:
                    if pin_dir == "input":
                        input_bits.append(str(bit))
                    else:
                        output_bits.append(str(bit))

            gates.append({
                "id": cell_name,
                "type": cell_type,
                "inputs": input_bits,
                "outputs": output_bits,
                "properties": {"original_type": cell.get("type", "")}
            })

        # Build wires from connections
        # Map: bit -> list of (gate_id, port_direction)
        bit_drivers = {}   # bit -> gate_id that drives it (output)
        bit_consumers = {} # bit -> [gate_ids that consume it] (input)

        for g in gates:
            for bit in g.get("outputs", []):
                bit_drivers[bit] = g["id"]
            for bit in g.get("inputs", []):
                if bit not in bit_consumers:
                    bit_consumers[bit] = []
                bit_consumers[bit].append(g["id"])

        for bit, driver in bit_drivers.items():
            for consumer in bit_consumers.get(bit, []):
                wires.append({
                    "from": driver,
                    "fromPort": f"out_{bit}",
                    "to": consumer,
                    "toPort": f"in_{bit}"
                })

    return gates, wires


@app.route("/synthesize", methods=["POST"])
def synthesize():
    try:
        data = request.get_json()
        verilog = data.get("verilog", "")
        yosys_settings = data.get("yosys_settings", {})
        abc_settings = data.get("abc_settings", {})
        tech_library = data.get("tech_library", [])

        if not verilog.strip():
            return jsonify({"error": "No Verilog code provided"}), 400

        with tempfile.TemporaryDirectory() as tmpdir:
            verilog_path = os.path.join(tmpdir, "input.v")
            liberty_path = os.path.join(tmpdir, "tech.lib")
            json_path = os.path.join(tmpdir, "output.json")
            script_path = os.path.join(tmpdir, "synth.ys")

            with open(verilog_path, "w") as f:
                f.write(verilog)

            with open(liberty_path, "w") as f:
                f.write(generate_liberty(tech_library))

            # Determine if we have flip-flops in the library
            has_ff = any(
                p.get("type", "").upper().startswith("DFF")
                for p in tech_library
            )

            # Build Yosys synthesis script
            flatten = yosys_settings.get("flatten", False)
            synth_cmd = yosys_settings.get("synthCommand", "synth")
            target_arch = yosys_settings.get("targetArch", "generic")

            script = f"read_verilog {verilog_path}\n"

            # Use architecture-specific synth if not generic
            if target_arch and target_arch != "generic":
                script += f"synth_{target_arch}\n"
            else:
                script += f"{synth_cmd}\n"

            if flatten:
                script += "flatten\n"

            script += "opt\n"

            if has_ff:
                script += f"dfflibmap -liberty {liberty_path}\n"

            script += f"abc -liberty {liberty_path}\n"
            script += "opt_clean -purge\n"
            script += f"write_json {json_path}\n"

            with open(script_path, "w") as f:
                f.write(script)

            result = subprocess.run(
                ["yosys", "-s", script_path],
                capture_output=True,
                text=True,
                timeout=120
            )

            if not os.path.exists(json_path):
                return jsonify({
                    "error": "Yosys synthesis failed",
                    "stderr": result.stderr[-2000:] if result.stderr else "",
                    "stdout": result.stdout[-2000:] if result.stdout else ""
                }), 400

            gates, wires = parse_yosys_json(json_path)

            return jsonify([gates, wires])

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Synthesis timed out (120s limit)"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/", methods=["GET", "HEAD"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
