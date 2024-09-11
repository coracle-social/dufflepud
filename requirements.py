import sys
import toml

with open(sys.argv[1]) as f:
    result = toml.load(f)

for package, constraint in result['tool']['poetry']['dependencies'].items():
    if package == 'python':
        continue

    print(package)
