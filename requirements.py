import sys
import toml

with open(sys.argv[1]) as f:
    result = toml.load(f)

for package, constraint in result['tool']['poetry']['dependencies'].items():
    if package == 'python':
        continue

    if constraint == '*':
        print(package)
    else:
        print(f'{package}=={constraint[1:]}')
