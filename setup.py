from setuptools import setup, find_packages

setup(
    name="agenthub",
    version="0.1.0",
    packages=find_packages(),
    python_requires=">=3.11",
    install_requires=[
        "anthropic>=0.39.0",
        "openai>=1.50.0",
        "pyyaml>=6.0",
        "rich>=13.0",
    ],
    entry_points={
        "console_scripts": [
            "agenthub=agenthub.cli:main",
        ],
    },
)
