#include <acl/acl.h>
#include <aclnnop/aclnn_add.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

void Check(aclError code, const char* operation)
{
    if (code != ACL_SUCCESS) {
        throw std::runtime_error(std::string(operation) + " returned " + std::to_string(code));
    }
}

uint64_t ParseBounded(const char* value, const char* name, uint64_t minimum, uint64_t maximum)
{
    std::string text(value);
    std::size_t consumed = 0;
    uint64_t parsed = 0;
    try {
        parsed = std::stoull(text, &consumed, 10);
    } catch (const std::exception&) {
        throw std::runtime_error(std::string(name) + " must be an unsigned integer");
    }
    if (consumed != text.size() || parsed < minimum || parsed > maximum) {
        throw std::runtime_error(std::string(name) + " is outside the supported range");
    }
    return parsed;
}

struct Resources {
    bool aclInitialized = false;
    bool deviceSet = false;
    int32_t device = -1;
    aclrtContext context = nullptr;
    aclrtStream stream = nullptr;
    std::vector<aclTensor*> tensors;
    aclScalar* scalar = nullptr;
    std::vector<void*> deviceBuffers;
    void* workspace = nullptr;

    ~Resources()
    {
        for (auto* tensor : tensors) {
            if (tensor != nullptr) {
                aclDestroyTensor(tensor);
            }
        }
        if (scalar != nullptr) {
            aclDestroyScalar(scalar);
        }
        if (workspace != nullptr) {
            aclrtFree(workspace);
        }
        for (auto* buffer : deviceBuffers) {
            if (buffer != nullptr) {
                aclrtFree(buffer);
            }
        }
        if (stream != nullptr) {
            aclrtDestroyStream(stream);
        }
        if (context != nullptr) {
            aclrtDestroyContext(context);
        }
        if (deviceSet) {
            aclrtResetDevice(device);
        }
        if (aclInitialized) {
            aclFinalize();
        }
    }
};

aclTensor* CreateTensor(Resources& resources, const std::vector<float>& hostData)
{
    const uint64_t bytes = hostData.size() * sizeof(float);
    void* deviceAddress = nullptr;
    Check(aclrtMalloc(&deviceAddress, bytes, ACL_MEM_MALLOC_HUGE_FIRST), "aclrtMalloc");
    resources.deviceBuffers.push_back(deviceAddress);
    Check(
        aclrtMemcpy(deviceAddress, bytes, hostData.data(), bytes, ACL_MEMCPY_HOST_TO_DEVICE),
        "aclrtMemcpy(H2D)");

    const std::vector<int64_t> shape = {static_cast<int64_t>(hostData.size())};
    const std::vector<int64_t> strides = {1};
    aclTensor* tensor = aclCreateTensor(
        shape.data(), shape.size(), ACL_FLOAT, strides.data(), 0, ACL_FORMAT_ND, shape.data(), shape.size(),
        deviceAddress);
    if (tensor == nullptr) {
        throw std::runtime_error("aclCreateTensor returned null");
    }
    resources.tensors.push_back(tensor);
    return tensor;
}

}  // namespace

int main(int argc, char** argv)
{
    try {
        if (argc != 4) {
            throw std::runtime_error("usage: npu_acl_smoke DEVICE ELEMENTS ITERATIONS");
        }
        const uint64_t deviceValue = ParseBounded(argv[1], "DEVICE", 0, 1023);
        const uint64_t elements = ParseBounded(argv[2], "ELEMENTS", 1, 16ULL * 1024 * 1024);
        const uint64_t iterations = ParseBounded(argv[3], "ITERATIONS", 1, 10000);
        if (deviceValue > static_cast<uint64_t>(std::numeric_limits<int32_t>::max())) {
            throw std::runtime_error("DEVICE is outside the supported range");
        }

        Resources resources;
        resources.device = static_cast<int32_t>(deviceValue);
        Check(aclInit(nullptr), "aclInit");
        resources.aclInitialized = true;
        Check(aclrtSetDevice(resources.device), "aclrtSetDevice");
        resources.deviceSet = true;
        Check(aclrtCreateContext(&resources.context, resources.device), "aclrtCreateContext");
        Check(aclrtSetCurrentContext(resources.context), "aclrtSetCurrentContext");
        Check(aclrtCreateStream(&resources.stream), "aclrtCreateStream");

        std::vector<float> self(elements);
        std::vector<float> other(elements);
        std::vector<float> output(elements, 0.0F);
        for (uint64_t index = 0; index < elements; ++index) {
            self[index] = static_cast<float>(index % 97) / 16.0F;
            other[index] = static_cast<float>(index % 31) / 8.0F;
        }
        float alphaValue = 1.25F;
        aclTensor* selfTensor = CreateTensor(resources, self);
        aclTensor* otherTensor = CreateTensor(resources, other);
        aclTensor* outputTensor = CreateTensor(resources, output);
        resources.scalar = aclCreateScalar(&alphaValue, ACL_FLOAT);
        if (resources.scalar == nullptr) {
            throw std::runtime_error("aclCreateScalar returned null");
        }

        uint64_t workspaceCapacity = 0;
        const auto started = std::chrono::steady_clock::now();
        for (uint64_t iteration = 0; iteration < iterations; ++iteration) {
            uint64_t workspaceSize = 0;
            aclOpExecutor* executor = nullptr;
            Check(
                aclnnAddGetWorkspaceSize(
                    selfTensor, otherTensor, resources.scalar, outputTensor, &workspaceSize, &executor),
                "aclnnAddGetWorkspaceSize");
            if (workspaceSize > workspaceCapacity) {
                if (resources.workspace != nullptr) {
                    Check(aclrtFree(resources.workspace), "aclrtFree(workspace)");
                    resources.workspace = nullptr;
                }
                Check(
                    aclrtMalloc(&resources.workspace, workspaceSize, ACL_MEM_MALLOC_HUGE_FIRST),
                    "aclrtMalloc(workspace)");
                workspaceCapacity = workspaceSize;
            }
            Check(aclnnAdd(resources.workspace, workspaceSize, executor, resources.stream), "aclnnAdd");
        }
        Check(aclrtSynchronizeStream(resources.stream), "aclrtSynchronizeStream");
        const auto ended = std::chrono::steady_clock::now();

        const uint64_t bytes = output.size() * sizeof(float);
        Check(
            aclrtMemcpy(
                output.data(), bytes, resources.deviceBuffers[2], bytes, ACL_MEMCPY_DEVICE_TO_HOST),
            "aclrtMemcpy(D2H)");
        float maxAbsoluteError = 0.0F;
        for (uint64_t index = 0; index < elements; ++index) {
            const float expected = self[index] + alphaValue * other[index];
            maxAbsoluteError = std::max(maxAbsoluteError, std::abs(output[index] - expected));
        }
        if (!std::isfinite(maxAbsoluteError) || maxAbsoluteError > 0.0001F) {
            throw std::runtime_error("result verification failed; max absolute error=" +
                                     std::to_string(maxAbsoluteError));
        }
        const double elapsedMs =
            std::chrono::duration<double, std::milli>(ended - started).count();
        std::cout << std::fixed << std::setprecision(6)
                  << "{\"status\":\"PASS\",\"device\":" << resources.device
                  << ",\"elements\":" << elements << ",\"iterations\":" << iterations
                  << ",\"elapsed_ms\":" << elapsedMs << ",\"max_abs_error\":"
                  << maxAbsoluteError << "}" << std::endl;
        return EXIT_SUCCESS;
    } catch (const std::exception& error) {
        std::cerr << "npu_acl_smoke failed: " << error.what() << std::endl;
        return EXIT_FAILURE;
    }
}
