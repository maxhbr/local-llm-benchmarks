#!/usr/bin/env nix-shell
#! nix-shell -i python3 -p python3Packages.openai python3Packages.pydantic

import json
import time
from openai import OpenAI
from pydantic import BaseModel, ValidationError, Field

# =====================================================================
# 1. SETUP ENGINE & TARGET MODELS
# =====================================================================
# Change the base_url if using vLLM (http://localhost:8000/v1) or LM Studio
client = OpenAI(base_url="http://thing.wg0.maxhbr.local:4000/v1", api_key="ollama")

# Define the models to test sequentially.
# One representative per (model, device) — highest available quant chosen.
MODELS_TO_TEST = [
    # --- Qwen3.6-35B-A3B (MoE) ---
    "gfx1151:Qwen3.6-35B-A3B-BF16",
    "rtx5090:Qwen3.6-35B-A3B",
    # --- Qwen3.6-27B (Dense) ---
    "gfx1151:Qwen3.6-27B-Q8_0",
    "rtx5090:Qwen3.6-27B-Q8_0",
    # --- Qwen3.6-35B (Dense) ---
    "gfx1151:Qwen3.6-35B",
    "rtx5090:Qwen3.6-35B",
    # --- Qwen3.5 ---
    "gfx1151:qwen3.5-122B-A10B-Q5_K_M",
    "gfx1151:Qwen3.5-9B-Q5_K_M",
    "rtx5090:Qwen3.5-9B-Q5_K_M",
    # --- NVIDIA Nemotron ---
    "gfx1151:NVIDIA-Nemotron-3-Nano-Omni-Q8_0",
    "gfx1151:NVIDIA-Nemotron-3-Super-120B-A12B-Q5_K_M",
    # --- MiniMax M2.7 ---
    "gfx1151:MiniMax-M2.7-UD-IQ4_NL",
    # --- gemma-4 ---
    "gfx1151:gemma-4-31B-BF16",
    "gfx1151:gemma-4-26B-A4B-it-UD-Q8_K_XL",
    "rtx5090:gemma-4-31B-Q5",
    "rtx5090:gemma-4-26B-Q8",
]

# =====================================================================
# 2. DEFINE THE EXPECTED STRUCTURED OUTPUT (The Tool Schema)
# =====================================================================
class ToolCallSchema(BaseModel):
    tool_name: str = Field(description="Must be exactly 'calculate_density'")
    mass_kg: float = Field(description="The mass extracted from text, converted to kg")
    volume_m3: float = Field(description="The volume extracted from text, converted to cubic meters")

# =====================================================================
# 3. MOCK ENVIRONMENT / TOOLS
# =====================================================================
def mock_calculate_density_tool(mass: float, volume: float) -> str:
    """Simulates the environment executing the tool requested by the model."""
    if volume <= 0:
        return "Error: Volume must be greater than 0."
    density = mass / volume
    return f"Success: Calculated density is {density:.2f} kg/m3."

# =====================================================================
# 4. BENCHMARK RUNNER
# =====================================================================
def run_agent_benchmark(model_name: str):
    print(f"\n{'='*60}\n🚀 RUNNING BENCHMARK: {model_name}\n{'='*60}")
    
    # SYSTEM PROMPT: Forces the local model to act like a strict JSON agent
    system_prompt = (
        "You are an agentic core framework. You must ONLY respond in a raw, valid JSON object matching "
        "the provided schema. Do not include markdown code blocks like ```json. Do not talk. "
        "Schema:\n" + json.dumps(ToolCallSchema.model_json_schema(), indent=2)
    )
    
    # USER PROMPT: Messy instructions with unit conversion tricks (g to kg, liters to m3)
    user_prompt = (
        "We need to find the density of the new alloy sample. The lab report states "
        "the mass is 4500 grams, and it displaces exactly 0.5 liters of water. "
        "Trigger the calculate_density tool with the correct SI units (kg and m3)."
    )
    
    history = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    metrics = {
        "turn_1_tps": 0.0,
        "json_valid": False,
        "math_conversion_correct": False,
        "turn_2_tps": 0.0,
        "final_success": False
    }

    # -----------------------------------------------------------------
    # TURN 1: Extract Data and Generate Structured Tool Call
    # -----------------------------------------------------------------
    print("[Turn 1] Requesting Structured Tool Call...")
    start_time = time.time()
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=history,
            temperature=0.0,  # CRITICAL: Keep it deterministic for benchmarking
            max_tokens=200
        )
        duration_t1 = time.time() - start_time
        raw_output = response.choices[0].message.content.strip()
        
        # Calculate raw rough tokens per second
        tokens_gen = response.usage.completion_tokens
        metrics["turn_1_tps"] = round(tokens_gen / duration_t1, 2)
        
        print(f" -> Received raw response in {duration_t1:.2f}s ({metrics['turn_1_tps']} tokens/sec)")
        print(f" -> Raw Output: {raw_output}")
        
        # Clean any accidental markdown code fences the model might have added
        cleaned_output = raw_output.replace("```json", "").replace("```", "").strip()
        
        # Validate JSON strict adherence
        tool_call = ToolCallSchema.model_validate_json(cleaned_output)
        metrics["json_valid"] = True
        print(" -> ✅ Step 1 Success: Valid JSON generated.")
        
        # Verify if the model successfully converted units (4500g -> 4.5kg; 0.5L -> 0.0005m3)
        if abs(tool_call.mass_kg - 4.5) < 1e-4 and abs(tool_call.volume_m3 - 0.0005) < 1e-6:
            metrics["math_conversion_correct"] = True
            print(" -> ✅ Step 2 Success: Accurate unit conversions.")
        else:
            print(f" -> ❌ Step 2 Fail: Incorrect parameters parsed (Mass: {tool_call.mass_kg}, Vol: {tool_call.volume_m3})")

    except ValidationError as e:
        print(f" -> ❌ Step 1 Fail: Broken JSON or missing fields.\nError: {e}")
        return metrics
    except Exception as e:
        print(f" -> ❌ API connection error: {e}")
        return metrics

    # -----------------------------------------------------------------
    # TURN 2: Simulate Tool Execution and Final Answer Synthesis
    # -----------------------------------------------------------------
    print("\n[Turn 2] Executing tool locally and feeding observation back to model...")
    # Execute the Python function using data parsed by the LLM
    tool_observation = mock_calculate_density_tool(tool_call.mass_kg, tool_call.volume_m3)
    print(f" -> Tool Output: {tool_observation}")
    
    # Append the cycle to the agent loop history
    history.append({"role": "assistant", "content": cleaned_output})
    history.append({"role": "user", "content": f"Tool Observation: {tool_observation}. Summarize the findings for the engineering team."})
    
    start_time = time.time()
    try:
        response_t2 = client.chat.completions.create(
            model=model_name,
            messages=history,
            temperature=0.3, # Marginally higher for final text generation summarization
            max_tokens=300
        )
        duration_t2 = time.time() - start_time
        metrics["turn_2_tps"] = round(response_t2.usage.completion_tokens / duration_t2, 2)
        final_summary = response_t2.choices[0].message.content.strip()
        
        print(f" -> Final Summary Received ({metrics['turn_2_tps']} tokens/sec):")
        print(f"\n{final_summary}\n")
        
        # Evaluation check: Did it reference the right calculated number (9000 kg/m3)?
        if "9000" in final_summary:
            metrics["final_success"] = True
            print(" -> ✅ Step 3 Success: State maintained through final calculation summary.")
        else:
            print(" -> ❌ Step 3 Fail: Final answer didn't contextualize the tool results correctly.")
            
    except Exception as e:
        print(f" -> ❌ Turn 2 failed: {e}")

    return metrics

# =====================================================================
# 5. EXECUTION MATRIX & BREAKDOWN
# =====================================================================
if __name__ == "__main__":
    results = {}
    for model in MODELS_TO_TEST:
        results[model] = run_agent_benchmark(model)
        
    print(f"\n\n{'='*60}\n📊 FINAL BENCHMARK SCOREBOARD\n{'='*60}")
    print(f"{'Model Name':<40} | {'T1 TPS':<7} | {'JSON?':<5} | {'Math?':<5} | {'Final?':<6}")
    print("-" * 75)
    for model, m in results.items():
        print(f"{model:<40} | {m['turn_1_tps']:<7} | {str(m['json_valid']):<5} | {str(m['math_conversion_correct']):<5} | {str(m['final_success']):<6}")
