#!/bin/bash
#
# Setup script for Trimble Native RINEX 3 Converter
#
# This script installs the Docker-based Trimble Convert to RINEX utility,
# which produces native RINEX 3 files from Trimble T00/T02 raw data.
#
# Requirements:
#   - Docker installed and running
#   - Internet connection (to pull Docker image)
#
# Usage:
#   ./setup.sh          # Install the Docker image
#   ./setup.sh --check  # Check if already installed
#   ./setup.sh --test   # Run a test conversion
#

set -e

# Configuration
DOCKER_IMAGE="trm2rinex:cli-light"
SOURCE_IMAGE="geodesyewsp/trm2rinex:cli-light"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

print_status() {
    echo -e "${GREEN}[OK]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_info() {
    echo -e "[INFO] $1"
}

# Check if Docker is installed and running
check_docker() {
    if ! command -v docker &> /dev/null; then
        print_error "Docker is not installed"
        echo "Please install Docker first:"
        echo "  https://docs.docker.com/engine/install/"
        return 1
    fi

    if ! docker info &> /dev/null; then
        print_error "Docker daemon is not running"
        echo "Please start Docker:"
        echo "  sudo systemctl start docker"
        return 1
    fi

    print_status "Docker is installed and running"
    return 0
}

# Check if the image is already installed
check_image() {
    if docker image inspect "$DOCKER_IMAGE" &> /dev/null; then
        print_status "Docker image '$DOCKER_IMAGE' is installed"
        return 0
    else
        print_warning "Docker image '$DOCKER_IMAGE' is not installed"
        return 1
    fi
}

# Check if Wine and convertToRinex work inside the container
check_converter() {
    print_info "Testing Wine and convertToRinex inside container..."

    output=$(docker run --rm --entrypoint="" "$DOCKER_IMAGE" \
        /opt/wine/bin/wine \
        "C:\\Program Files\\Trimble\\convertToRINEX\\convertToRinex.exe" \
        --help 2>&1 || true)

    if echo "$output" | grep -q "No input file specified"; then
        print_status "convertToRinex is working"
        return 0
    else
        print_error "convertToRinex test failed"
        echo "$output"
        return 1
    fi
}

# Install the Docker image
install_image() {
    print_info "Pulling Docker image from Docker Hub..."
    print_info "Source: $SOURCE_IMAGE"
    print_info "This may take a few minutes (image is ~2.4 GB)..."

    if docker pull "$SOURCE_IMAGE"; then
        print_status "Image pulled successfully"
    else
        print_error "Failed to pull image"
        return 1
    fi

    print_info "Tagging image as '$DOCKER_IMAGE'..."
    docker tag "$SOURCE_IMAGE" "$DOCKER_IMAGE"
    print_status "Image tagged as '$DOCKER_IMAGE'"

    return 0
}

# Run a test conversion
run_test() {
    print_info "Running test conversion..."

    # Create a minimal test (just check the tool responds)
    if check_converter; then
        print_status "Test passed - converter is ready to use"
        echo ""
        echo "Usage with receivers CLI:"
        echo "  receivers rinex STATION --native-trimble -d 1"
        echo ""
        echo "Or directly with Python:"
        echo "  from receivers.rinex import TrimbleNativeConverter"
        echo "  converter = TrimbleNativeConverter('MANA')"
        echo "  result = converter.convert_file('file.T02', output_dir)"
        return 0
    else
        print_error "Test failed"
        return 1
    fi
}

# Show status
show_status() {
    echo "=== Trimble Native Converter Status ==="
    echo ""

    check_docker || exit 1

    if check_image; then
        check_converter
        echo ""
        echo "The converter is ready to use."
    else
        echo ""
        echo "Run './setup.sh' to install the converter."
    fi
}

# Main
case "${1:-}" in
    --check)
        show_status
        ;;
    --test)
        check_docker || exit 1
        check_image || { print_error "Image not installed. Run './setup.sh' first."; exit 1; }
        run_test
        ;;
    --help|-h)
        echo "Trimble Native RINEX 3 Converter Setup"
        echo ""
        echo "Usage:"
        echo "  $0           Install the Docker image"
        echo "  $0 --check   Check installation status"
        echo "  $0 --test    Run a test conversion"
        echo "  $0 --help    Show this help"
        echo ""
        echo "Requirements:"
        echo "  - Docker installed and running"
        echo "  - ~3 GB disk space for the Docker image"
        ;;
    *)
        echo "=== Trimble Native RINEX 3 Converter Setup ==="
        echo ""

        check_docker || exit 1

        if check_image; then
            print_info "Image already installed"
            check_converter
        else
            install_image || exit 1
            check_converter || exit 1
        fi

        echo ""
        print_status "Setup complete!"
        echo ""
        echo "Usage:"
        echo "  receivers rinex STATION --native-trimble -d 1"
        ;;
esac
