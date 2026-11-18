import sys

sys.path.insert(0, ".")
from backtest.engine import load_data

_, _, bias_map, _, _, _, _ = load_data(12)

print("Istanbul for bias values:")
for source in ["openweathermap", "visual_crossing"]:
    bias = bias_map.get(("Istanbul", source))
    print(f"  {source}: {bias}")

print("\nAll Istanbul sources:")
for (city, source), bias in bias_map.items():
    if city == "Istanbul":
        print(f"  {source}: {bias}")

print(f"\nTotal cities count: {len(set(city for city, _ in bias_map.keys()))}")
print(f"Total sources count: {len(set(source for _, source in bias_map.keys()))}")
