from flask import Flask, request, jsonify
from flask_cors import CORS
import tempfile
import os
import subprocess
import json

app = Flask(__name__)
CORS(app)


def generate_liberty(primitives):
    """
    Generate a simple combinational Liberty that ABC can parse.

    Notes:
    - ABC/Yosys are picky: keep this minimal.
    - For combinational cells, put boolean 'function' on the OUTPUT pin.
    - Use simple input pin naming A,B,C...
    """
    lines = []
    lines.append("library(tech) {")
    lines.append('  delay_model : table_lookup;')
    lines.append('  time_unit : "1ns";')
    lines.append('  voltage_unit : "1V";')
    lines.append('  current_unit : "1mA";')
    lines.append('  pulling_resistance_unit : "1kohm";')
    lines.append('  capacitive_load_unit(1,pf);')
    lines.append("")

    for prim in primitives:
        name = prim["name"]
        func = prim.get("function", "")
        area = prim.get("area", 1.0)
        num_inputs = int(prim.get("numInputs", 1))

        lines.append(f"  cell({name}) {{")
        lines.append(f"    area : {area};")

        pin_names = [chr(65 + i) for i in range(num_inputs)]
        for pin in pin_names:
            lines.append(f"    pin({pin}) {{")
            lines.append("      direction : input;")
            lines.append("      capacitance : 0.01;")
            lines.append("    }")

        lines.append("    pin(Y) {")
        lines.append("      direction : output;")
        # Output capacitance is not really correct, but harmless for pure Boolean mapping.
        lines.append("      capacitance : 0.01;")
        if func:
            # Ensure it is quoted
            lines.append(f'      function : "{func}";')
        lines.append("    }")
        lines.append("  }")
        lines.append("")

    lines.append("}")
    return "\n".join(lines)


def build_abc_script(abc_settings):
    """
    Build ABC commands for optimization/mapping.

    IMPORTANT:
    - Do NOT 'read_liberty' here. Let Yosys provide the liberty via:
        abc -liberty tech.lib -script abc.script
      That keeps Yosys' ABC integration consistent and avoids a bunch of edge cases.
    """
    opt_cmd = abc_settings.get("optimizationCommand", "resyn2")
    optimize_for = abc_settings.get("optimizeFor", "balanced")
    custom = abc_settings.get("customScript", "")

    # Treat as "default" placeholder if it matches common template fragments
    default_markers = ["# Custom ABC commands", "resyn2", "map"]
    is_default = any(m in custom for m in default_markers)

    has_custom = bool(custom and not is_default and any(
        line.strip() and not line.strip().startswith("#")
        for line in custom.strip().splitlines()
    ))

    cmds = []

    if has_custom:
        for line in custom.strip().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                cmds.append(line)
        # If user forgot to call map, do it (mapping must happen).
        if not any(line.split()[0] in ("map", "amap") for line in cmds if line):
            cmds.append("map")
    else:
        # A safe, common baseline for AIG rewriting
        # (You can tune this, but keep it simple while debugging.)
        cmds.append("strash")
        cmds.append(opt_cmd)

        # For liberty cell mapping, 'map' is usually the right default.
        # 'amap' can be more fragile depending on network/library details.
        cmds.append("map")

        # If you *really* want to try area/delay variants later, do it here,
        # but keep 'map' for stability.

    return "\n".join(cmds) + "\n"


def parse_yosys_json(json_path):
    with open(json_path, "r") as f:
        data = json.load(f)

    gates = []
    wires = []

    for mod_name, mod in data.get("modules", {}).items():
        # Ports -> pseudo IO gates
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
                        "name": f"{port_name}[{i}]" if len(port.get("bits", [])) > 1 else port_name
                    }
                })

        # Cells
        for cell_name, cell in mod.get("cells", {}).items():
            cell_type = cell.get("type", "BUF").lstrip("$_").rstrip("_")
            connections = cell.get("connections", {})

            input_bits = []
            output_bits = []
            port_dirs = cell.get("port_directions", {})

            for pin_name, bits in connections.items():
                pin_dir = port_dirs.get(pin_name, "input")
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

        # Wires: driver->consumer per bit
        bit_drivers = {}
        bit_consumers = {}

        for g in gates:
            for bit in g.get("outputs", []):
                bit_drivers[bit] = g["id"]
            for bit in g.get("inputs", []):
                bit_consumers.setdefault(bit, []).append(g["id"])

        for bit, driver in bit_drivers.items():
            for consumer in bit_consumers.get(bit, []):
                wires.append({
                    "from": driver,
                    "fromPort": f"out_{bit}",
                    "to": consumer,
                    "toPort": f"in_{bit}",
                })

    return gates, wires


@app.route("/synthesize", methods=["POST"])
def synthesize():
    try:
        data = request.get_json() or {}
        verilog = data.get("verilog", "")
        yosys_settings = data.get("yosys_settings", {}) or {}
        abc_settings = data.get("abc_settings", {}) or {}
        tech_library = data.get("tech_library", []) or []

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

            # ABC script: optimization + map, but NO read_liberty
            abc_script = build_abc_script(abc_settings)
            with open(abc_script_path, "w") as f:
                f.write(abc_script)

            # Flip-flop library presence
            has_ff = any(
                str(p.get("type", "")).upper().startswith("DFF")
                for p in tech_library
            )

            # Optimization level -> -O flag (for synth)
            opt_level = yosys_settings.get("optimizationLevel", "medium")
            level_map = {"none": "0", "low": "1", "medium": "2", "high": "3"}
            opt_flag = f"-O{level_map.get(opt_level, '2')}"

            flatten = bool(yosys_settings.get("flatten", False))
            custom_yosys = yosys_settings.get("customScript", "")

            # If custom Yosys is provided, we still enforce the critical ordering around abc.
            has_custom_yosys = bool(custom_yosys and any(
                line.strip() and not line.strip().startswith("#")
                for line in custom_yosys.strip().splitlines()
            ))

            if has_custom_yosys:
                script_lines = []
                custom_lines = custom_yosys.strip()

                # Ensure we read the design
                if "read_verilog" not in custom_lines:
                    script_lines.append(f"read_verilog {verilog_path}")
                script_lines.append(custom_lines)

                # Ensure design is in a good state before ABC
                # (These are usually safe even if already done.)
                script_lines += [
                    "proc; opt",
                    "memory; opt",
                    "techmap; opt",
                ]

                if flatten:
                    script_lines.append("flatten")

                if has_ff:
                    script_lines.append(f"dfflibmap -liberty {liberty_path}")

                # Critical: use -liberty here (do not rely on read_liberty in abc script)
                # Optional debugging flags (enable if needed):
                #   -showtmp -nocleanup
                script_lines.append(
                    f"abc -liberty {liberty_path} -script {abc_script_path}"
                )

                script_lines.append("opt_clean -purge")

                if "write_json" not in custom_lines:
                    script_lines.append(f"write_json {json_path}")

                script = "\n".join(script_lines) + "\n"

            else:
                # Standard synthesis flow, but we control ordering.
                synth_cmd = yosys_settings.get("synthCommand", "synth")

                script_lines = [
                    f"read_verilog {verilog_path}",
                ]

                # Run synth without abc (we will call abc ourselves with -liberty)
                cmd = synth_cmd
                if "-noabc" not in cmd:
                    cmd += " -noabc"
                # Add opt flag if user didn't already
                if "-O" not in cmd:
                    cmd += f" {opt_flag}"
                script_lines.append(cmd)

                if flatten:
                    script_lines.append("flatten")

                # Additional cleanup/normalization before mapping
                script_lines += [
                    "proc; opt",
                    "memory; opt",
                    "techmap; opt",
                    "opt",
                ]

                if has_ff:
                    script_lines.append(f"dfflibmap -liberty {liberty_path}")

                # Critical: use -liberty
                script_lines.append(
                    f"abc -liberty {liberty_path} -script {abc_script_path}"
                )

                script_lines += [
                    "opt_clean -purge",
                    f"write_json {json_path}",
                ]

                script = "\n".join(script_lines) + "\n"

            with open(script_path, "w") as f:
                f.write(script)

            # Even if your FS is fine, forcing temp files into tmpdir
            # makes ABC temp behavior more deterministic.
            env = os.environ.copy()
            env["TMPDIR"] = tmpdir
            env["TMP"] = tmpdir
            env["TEMP"] = tmpdir

            result = subprocess.run(
                ["yosys", "-s", script_path],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )

            if not os.path.exists(json_path):
                return jsonify({
                    "error": "Yosys synthesis failed",
                    "stderr": (result.stderr or "")[-4000:],
                    "stdout": (result.stdout or "")[-4000:],
                    "yosys_script": script,  # helpful when debugging
                    "abc_script": abc_script,
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
