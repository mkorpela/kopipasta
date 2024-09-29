from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="kopipasta",
    version="0.5.0",
    author="Mikko Korpela",
    author_email="mikko.korpela@gmail.com",
    description="A CLI tool to generate prompts with project structure and file contents",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/mkorpela/kopipasta",
    packages=find_packages(),
    install_requires=[
        "pyperclip==1.9.0",
        "requests==2.32.3",
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.8",
    license="MIT",
    entry_points={
        "console_scripts": [
            "kopipasta=kopipasta.main:main",
        ],
    },
)
