// Does HIP run hipLaunchHostFunc callbacks from more than one thread?
// hipFile's async path puts the ENTIRE pread/pwrite inside a host function, so
// if HIP services host funcs from a single thread, all async hipFile I/O in a
// process is serialised no matter how many streams the caller uses.
#include <hip/hip_runtime.h>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <mutex>
#include <set>
#include <thread>

static std::mutex mu;
static std::set<std::thread::id> tids;
static std::atomic<int> concurrent{0}, max_concurrent{0};

static void cb(void *) {
    { std::lock_guard<std::mutex> g(mu); tids.insert(std::this_thread::get_id()); }
    int c = ++concurrent;
    int prev = max_concurrent.load();
    while (c > prev && !max_concurrent.compare_exchange_weak(prev, c)) {}
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    --concurrent;
}

int main(int argc, char **argv) {
    int nstreams = argc > 1 ? atoi(argv[1]) : 8;
    int per = argc > 2 ? atoi(argv[2]) : 4;
    std::vector<hipStream_t> s(nstreams);
    for (auto &x : s) hipStreamCreate(&x);
    auto t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < per; i++)
        for (int j = 0; j < nstreams; j++)
            hipLaunchHostFunc(s[j], cb, nullptr);
    for (auto &x : s) hipStreamSynchronize(x);
    auto ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    printf("streams=%d ops/stream=%d total_ops=%d\n", nstreams, per, nstreams * per);
    printf("wall=%.0f ms  (fully serial would be %d ms, fully parallel %d ms)\n",
           ms, nstreams * per * 100, per * 100);
    printf("distinct callback threads=%zu  max_concurrent_callbacks=%d\n",
           tids.size(), max_concurrent.load());
    return 0;
}
