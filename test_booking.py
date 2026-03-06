"""Quick end-to-end test - run from c:/call agent directory"""
import os, sys, json
sys.path.insert(0, 'c:/call agent')
os.chdir('c:/call agent')

from dotenv import load_dotenv
load_dotenv()

# Patch _execute_tool to print what it receives and returns
import services.agent as agent_mod
_orig_execute = agent_mod._execute_tool
def _debug_execute(tool_name, args, hospital_config):
    print(f"  [TOOL CALL] {tool_name}({json.dumps(args)})")
    result = _orig_execute(tool_name, args, hospital_config)
    print(f"  [TOOL RESULT] {json.dumps(result)[:200]}")
    return result
agent_mod._execute_tool = _debug_execute

from services.agent import run_agent_turn

with open('config/hospitals.json') as f:
    hospital = json.load(f)['hospitals']['aiims-bbsr-001']

history = []
turns = [
    "Dr. Das ke saath appointment chahiye",
    "Mera naam Rohan Purohit hai",
    "2026-03-07 ke liye",
    "9 baje wala time theek hai",
]

for i, msg in enumerate(turns):
    print(f"\n--- Turn {i+1} ---")
    print(f"Patient : {msg}")
    result = run_agent_turn(msg, history, hospital)
    print(f"Agent   : {result['response_text']}")
    print(f"Lang    : {result['detected_lang']}")
    print(f"Booked  : {result['appointment_booked']}")
    if result['booking_result']:
        print(f"Booking : {json.dumps(result['booking_result'], indent=2)}")
    if result['appointment_booked']:
        print("\n✓ APPOINTMENT SUCCESSFULLY BOOKED IN GOOGLE CALENDAR")
        break
