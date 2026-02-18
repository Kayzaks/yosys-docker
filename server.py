from flask import Flask, request, jsonify
from flask_cors import CORS
import tempfile
import os
import subprocess
import json

app = Flask(__name__)
CORS(app)


def generate_liberty(primitives):
    """Generate a Liberty .lib file that ABC can actually parse."""
    lines = []
    lines.append("library(tech) {")
    lines.append("  delay_model : table_lookup;")
    lines.append('  time_unit : "1ns";')
    lines.append('  capacity_unit : "1pf";')
    lines.append("")

    for prim in primitives:
        name = prim["name"]
        func = prim.get("function", "")
        area = prim.get("area", 1.0)
        num_inputs = prim.get("numInputs", 1)

        lines.append(f"  cell({name}) {{")
        lines.append(f"    area : {area};")

        pin_names = [chr(65 + i) for i in range(num_inputs)]
        for pin in pin_names:
            lines.append(f"    pin({pin}) {{")
            lines.append(f"      direction : input;")
            lines.append(f"      capacitance : 0.01;")
            lines.append(f"    }}")

        lines.append(f"    pin(Y) {{")
        lines.append(f"      direction : output;")
        lines.append(f"      capacitance : 0.01;")
        if func:
            lines.append(f'      function : "{func}";')
        lines.append(f"    }}")
        lines.append(f"  }}")
        lines.append("")

    lines.append("}")
    return "\n".join(lines)


def parse_yosys_json(json_path):
    with open(json_path, "r") as f:
        data = json.load(f)

    gates = []
    wires = []

    for mod_name, mod in data.get("modules", {}).items():
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
                    "properties": {
                        "name": f"{port_name}[{i}]"
                        if len(port.get("bits", [])) > 1
                        else port_name
                    },
                })

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
                "properties": {"original_type": cell.get("type", "")},
            })

        bit_drivers = {}
        bit_consumers = {}

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
                    "toPort": f"in_{bit}",
                })

    return gates, wires


def build_abc_script(liberty_path, abc_settings):
    """Build an ABC command string from abc_settings."""
    custom_script = abc_settings.get("customScript", "").strip()
    if custom_script:
        # User provided a full custom ABC script — use it directly
        return custom_script

    opt_cmd = abc_settings.get("optimizationCommand", "")
    optimize_for = abc_settings.get("optimizeFor", "balanced")

    # Map optimizeFor to ABC flags
    abc_cmds = []
    if opt_cmd:
        # Use the specified optimization command (e.g. "resyn2", "dc2", "compress2rs")
        abc_cmds.append(opt_cmd)
    else:
        # Default optimization based on target
        if optimize_for == "area":
            abc_cmds.append("strash; dch; amap")
        elif optimize_for == "speed":
            abc_cmds.append("strash; dc2; map")
        else:
            # balanced
            abc_cmds.append("strash; dc2; amap")

    return "; ".join(abc_cmds)


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
            abc_script_path = os.path.join(tmpdir, "abc.script")

            with open(verilog_path, "w") as f:
                f.write(verilog)

            with open(liberty_path, "w") as f:
                f.write(generate_liberty(tech_library))

            has_ff = any(
                p.get("type", "").upper().startswith("DFF") for p in tech_library
            )

            # --- Yosys settings ---
            flatten = yosys_settings.get("flatten", False)
            synth_cmd = yosys_settings.get("synthCommand", "synth")
            target_arch = yosys_settings.get("targetArch", "generic")
            opt_level = yosys_settings.get("optimizationLevel", "default")
            custom_script = yosys_settings.get("customScript", "").strip()

            if custom_script:
                # User provided a full custom Yosys script — use it,
                # but ensure read_verilog and write_json are present
                script = ""
                if "read_verilog" not in custom_script:
                    script += f"read_verilog {verilog_path}\n"
                script += custom_script + "\n"
                if "write_json" not in custom_script:
                    script += f"write_json {json_path}\n"
            else:
                # Build script from settings
                script = f"read_verilog {verilog_path}\n"

                # Synth command with architecture and optimization level
                if target_arch and target_arch != "generic":
                    synth_line = f"synth_{target_arch} -noabc"
                else:
                    synth_line = f"{synth_cmd} -noabc"

                # Append optimization level if not default
                if opt_level and opt_level != "default":
                    # Yosys synth supports -O0, -O1, -O2, -O3
                    level_map = {"none": "0", "low": "1", "medium": "2", "high": "3"}
                    level_num = level_map.get(opt_level, "")
                    if level_num:
                        synth_line += f" -O{level_num}"

                script += synth_line + "\n"

                if flatten:
                    script += "flatten\n"

                script += "opt\n"

                if has_ff:
                    script += f"dfflibmap -liberty {liberty_path}\n"

                # --- ABC settings ---
                abc_cmds = build_abc_script(liberty_path, abc_settings)
                with open(abc_script_path, "w") as f:
                    f.write(abc_cmds)

                script += f"abc -liberty {liberty_path} -script {abc_script_path}\n"
                script += "opt_clean -purge\n"
                script += f"write_json {json_path}\n"

            with open(script_path, "w") as f:
                f.write(script)

            result = subprocess.run(
                ["yosys", "-s", script_path],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if not os.path.exists(json_path):
                return jsonify({
                    "error": "Yosys synthesis failed",
                    "stderr": result.stderr[-2000:] if result.stderr else "",
                    "stdout": result.stdout[-2000:] if result.stdout else "",
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
