/* Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
 *
 * SPDX-License-Identifier: MIT
 *
 * Correctness test for hipFile's batch API.
 *
 * Upstream hipFile at bd0bc233 accepts batch submissions and then does nothing
 * with them: BatchContext::submit_operations validates the params and parks
 * them, and GetStatus/Cancel/Destroy all throw "Not Implemented".  This tree
 * carries patches/hipfile/01-hipfile-batch-worker-pool.patch to implement them.
 * Nothing in fio or nixlbench distinguishes "moved the bytes" from "claimed to
 * move the bytes and returned success", so that has to be checked here, by
 * writing a known pattern through the batch path and reading it back.
 *
 * What each case is actually for:
 *
 *   write/read        the base case, and the only one that proves data moved
 *                     rather than status codes being manufactured.
 *   partial reap      GetStatus with a max smaller than the number outstanding
 *                     must return only that many and leave the rest reapable;
 *                     an implementation that reaped everything into a caller's
 *                     short array would corrupt the heap rather than fail.
 *   min_nr=0          fio's reap path calls with min 0 and expects a prompt
 *                     return, not a block, whenever nothing has completed.
 *   timeout           a deadline that expires must return 0 events and success,
 *                     not an error and not a hang.
 *   cancel            operations that have not started come back as
 *                     hipFileCanceled; ones already running still report their
 *                     true outcome.  Both must still be reapable, or a caller
 *                     that cancels leaks context capacity.
 *   oversubscribe     submitting past the context capacity must be refused as a
 *                     unit, leaving the already-outstanding operations intact.
 *
 * Exit status is the number of failed cases, so it doubles as a build gate.
 */

#include <hip/hip_runtime.h>
#include <hipfile.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

int failures = 0;
int checks   = 0;

void
check(bool ok, const char *what)
{
    checks++;
    if (!ok) {
        failures++;
        std::printf("  FAIL: %s\n", what);
    }
    else {
        std::printf("  ok:   %s\n", what);
    }
}

bool
hipfile_ok(hipFileError_t s, const char *what)
{
    if (s.err != hipFileSuccess) {
        std::printf("  FAIL: %s -> err=%d hip=%d\n", what, s.err, s.hip_drv_err);
        return false;
    }
    return true;
}

// O_DIRECT demands aligned lengths and offsets; every size here is a multiple.
constexpr size_t BLOCK = 1u << 20;

struct Fixture {
    std::string  path;
    int          fd{-1};
    hipFileHandle_t fh{};
    void        *dev{nullptr};
    size_t       dev_bytes{0};
    bool         registered_buf{false};
    bool         registered_fh{false};

    bool setup(const std::string &dir, unsigned slots)
    {
        path      = dir + "/hipfile-batch-smoke.bin";
        dev_bytes = BLOCK * slots;

        fd = ::open(path.c_str(), O_CREAT | O_RDWR | O_DIRECT | O_TRUNC, 0644);
        if (fd < 0) {
            std::printf("  FAIL: open(%s, O_DIRECT): %s\n", path.c_str(), std::strerror(errno));
            return false;
        }
        if (::ftruncate(fd, static_cast<off_t>(dev_bytes)) != 0) {
            std::printf("  FAIL: ftruncate: %s\n", std::strerror(errno));
            return false;
        }

        hipFileDescr_t descr{};
        descr.handle.fd = fd;
        descr.type      = hipFileHandleTypeOpaqueFD;
        if (!hipfile_ok(hipFileHandleRegister(&fh, &descr), "hipFileHandleRegister")) {
            return false;
        }
        registered_fh = true;

        if (hipMalloc(&dev, dev_bytes) != hipSuccess) {
            std::printf("  FAIL: hipMalloc(%zu)\n", dev_bytes);
            return false;
        }
        if (!hipfile_ok(hipFileBufRegister(dev, dev_bytes, 0), "hipFileBufRegister")) {
            return false;
        }
        registered_buf = true;
        return true;
    }

    ~Fixture()
    {
        if (registered_buf) {
            hipFileBufDeregister(dev);
        }
        if (dev) {
            hipFree(dev);
        }
        if (registered_fh) {
            hipFileHandleDeregister(fh);
        }
        if (fd >= 0) {
            ::close(fd);
        }
        if (!path.empty()) {
            ::unlink(path.c_str());
        }
    }
};

void
fill_params(hipFileIOParams_t &p, hipFileHandle_t fh, void *base, size_t slot, hipFileOpcode_t op)
{
    p.mode                  = hipFileBatch;
    p.fh                    = fh;
    p.opcode                = op;
    p.u.batch.devPtr_base   = base;
    p.u.batch.devPtr_offset = static_cast<int64_t>(slot * BLOCK);
    p.u.batch.file_offset   = static_cast<int64_t>(slot * BLOCK);
    p.u.batch.size          = BLOCK;
    p.cookie                = reinterpret_cast<void *>(slot + 1);
}

/// Reap until `want` events have been collected or `attempts` polls pass.
unsigned
reap_all(hipFileBatchHandle_t batch, unsigned want, std::vector<hipFileIOEvents_t> &out)
{
    unsigned got = 0;
    for (int attempt = 0; attempt < 200 && got < want; attempt++) {
        unsigned  nr = want - got;
        timespec  ts{0, 50 * 1000 * 1000};
        auto      s = hipFileBatchIOGetStatus(batch, 1, &nr, out.data() + got, &ts);
        if (s.err != hipFileSuccess) {
            std::printf("  FAIL: GetStatus err=%d\n", s.err);
            return got;
        }
        got += nr;
    }
    return got;
}

} // namespace

int
main(int argc, char **argv)
{
    const std::string dir = argc > 1 ? argv[1] : "/tmp";
    constexpr unsigned SLOTS = 8;

    std::printf("hipFile batch smoke test, dir=%s\n", dir.c_str());

    if (!hipfile_ok(hipFileDriverOpen(), "hipFileDriverOpen")) {
        return 1;
    }

    {
        Fixture fx;
        if (!fx.setup(dir, SLOTS)) {
            hipFileDriverClose();
            return 1;
        }

        std::vector<hipFileIOParams_t> params(SLOTS);
        std::vector<hipFileIOEvents_t> events(SLOTS);

        // ---- write a known pattern through the batch path ------------------
        std::printf("[write/read]\n");
        std::vector<unsigned char> host(fx.dev_bytes);
        for (size_t i = 0; i < host.size(); i++) {
            host[i] = static_cast<unsigned char>((i * 31 + 7) & 0xff);
        }
        check(hipMemcpy(fx.dev, host.data(), host.size(), hipMemcpyHostToDevice) == hipSuccess,
              "seed device buffer");

        hipFileBatchHandle_t batch{};
        check(hipFileBatchIOSetUp(&batch, SLOTS).err == hipFileSuccess, "BatchIOSetUp");

        for (unsigned i = 0; i < SLOTS; i++) {
            fill_params(params[i], fx.fh, fx.dev, i, hipFileBatchWrite);
        }
        check(hipFileBatchIOSubmit(batch, SLOTS, params.data(), 0).err == hipFileSuccess,
              "BatchIOSubmit writes");

        unsigned got = reap_all(batch, SLOTS, events);
        check(got == SLOTS, "reaped every write");

        bool all_complete = true, all_full = true;
        unsigned long cookie_sum = 0;
        for (unsigned i = 0; i < got; i++) {
            all_complete &= (events[i].status == hipFileComplete);
            all_full &= (events[i].ret == BLOCK);
            cookie_sum += reinterpret_cast<unsigned long>(events[i].cookie);
        }
        check(all_complete, "every write reported hipFileComplete");
        check(all_full, "every write reported a full-length transfer");
        check(cookie_sum == (SLOTS * (SLOTS + 1)) / 2, "cookies round-tripped exactly once each");

        // Zero the device buffer so a read that moves nothing cannot pass.
        check(hipMemset(fx.dev, 0, fx.dev_bytes) == hipSuccess, "zero device buffer");

        for (unsigned i = 0; i < SLOTS; i++) {
            fill_params(params[i], fx.fh, fx.dev, i, hipFileBatchRead);
        }
        check(hipFileBatchIOSubmit(batch, SLOTS, params.data(), 0).err == hipFileSuccess,
              "BatchIOSubmit reads");
        got = reap_all(batch, SLOTS, events);
        check(got == SLOTS, "reaped every read");

        std::vector<unsigned char> back(fx.dev_bytes, 0);
        check(hipMemcpy(back.data(), fx.dev, back.size(), hipMemcpyDeviceToHost) == hipSuccess,
              "copy back");
        check(back == host, "data read back matches data written");

        // ---- partial reap ---------------------------------------------------
        std::printf("[partial reap]\n");
        for (unsigned i = 0; i < SLOTS; i++) {
            fill_params(params[i], fx.fh, fx.dev, i, hipFileBatchRead);
        }
        check(hipFileBatchIOSubmit(batch, SLOTS, params.data(), 0).err == hipFileSuccess,
              "submit for partial reap");
        {
            unsigned nr = 3;
            timespec ts{5, 0};
            auto     s   = hipFileBatchIOGetStatus(batch, 3, &nr, events.data(), &ts);
            check(s.err == hipFileSuccess, "partial GetStatus succeeded");
            check(nr <= 3, "partial GetStatus honoured the caller's array size");
            unsigned rest = reap_all(batch, SLOTS - nr, events);
            check(rest == SLOTS - nr, "remaining operations were still reapable");
        }

        // ---- min_nr = 0 must not block -------------------------------------
        std::printf("[min_nr=0]\n");
        {
            unsigned nr = SLOTS;
            auto     s  = hipFileBatchIOGetStatus(batch, 0, &nr, events.data(), nullptr);
            check(s.err == hipFileSuccess, "min_nr=0 with nothing outstanding returned success");
            check(nr == 0, "min_nr=0 with nothing outstanding returned no events");
        }

        // ---- expiring timeout ----------------------------------------------
        std::printf("[timeout]\n");
        {
            unsigned nr = SLOTS;
            timespec ts{0, 20 * 1000 * 1000};
            auto     s = hipFileBatchIOGetStatus(batch, SLOTS, &nr, events.data(), &ts);
            check(s.err == hipFileSuccess, "expired timeout returned success");
            check(nr == 0, "expired timeout returned no events");
        }

        // ---- cancel ---------------------------------------------------------
        std::printf("[cancel]\n");
        for (unsigned i = 0; i < SLOTS; i++) {
            fill_params(params[i], fx.fh, fx.dev, i, hipFileBatchRead);
        }
        check(hipFileBatchIOSubmit(batch, SLOTS, params.data(), 0).err == hipFileSuccess,
              "submit for cancel");
        check(hipFileBatchIOCancel(batch).err == hipFileSuccess, "BatchIOCancel");
        got = reap_all(batch, SLOTS, events);
        check(got == SLOTS, "every cancelled operation was still reapable");
        {
            // Whether an individual operation was cancelled or completed is a
            // race with the worker pool and not something to assert on. What
            // must hold is that none of them are still pending and none are in
            // some state the caller has no name for.
            bool terminal = true;
            for (unsigned i = 0; i < got; i++) {
                terminal &= (events[i].status == hipFileCanceled || events[i].status == hipFileComplete ||
                             events[i].status == hipFileFailed);
            }
            check(terminal, "cancelled operations reached a terminal status");
        }

        // ---- oversubscribe --------------------------------------------------
        std::printf("[oversubscribe]\n");
        {
            std::vector<hipFileIOParams_t> too_many(SLOTS + 1);
            for (unsigned i = 0; i < SLOTS + 1; i++) {
                fill_params(too_many[i], fx.fh, fx.dev, i % SLOTS, hipFileBatchRead);
            }
            auto s = hipFileBatchIOSubmit(batch, SLOTS + 1, too_many.data(), 0);
            check(s.err == hipFileInvalidValue, "submitting past capacity was refused");

            // The context must still be usable afterwards.
            check(hipFileBatchIOSubmit(batch, 1, params.data(), 0).err == hipFileSuccess,
                  "context still usable after a refused submission");
            check(reap_all(batch, 1, events) == 1, "and still completes work");
        }

        hipFileBatchIODestroy(batch);
        std::printf("  ok:   BatchIODestroy returned\n");
        checks++;

        // ---- destroy with work in flight ------------------------------------
        // The interesting case is not a quiet context but a busy one: Destroy
        // must not return while a worker can still write into an operation the
        // context owns.  A leak or a use-after-free here shows up as a crash
        // under this loop rather than as a wrong number.
        std::printf("[destroy under load]\n");
        bool survived = true;
        for (int round = 0; round < 20 && survived; round++) {
            hipFileBatchHandle_t b{};
            if (hipFileBatchIOSetUp(&b, SLOTS).err != hipFileSuccess) {
                survived = false;
                break;
            }
            for (unsigned i = 0; i < SLOTS; i++) {
                fill_params(params[i], fx.fh, fx.dev, i, hipFileBatchRead);
            }
            if (hipFileBatchIOSubmit(b, SLOTS, params.data(), 0).err != hipFileSuccess) {
                survived = false;
            }
            hipFileBatchIODestroy(b);
        }
        check(survived, "20 rounds of submit-then-immediately-destroy");
    }

    hipFileDriverClose();

    std::printf("\n%d/%d checks passed\n", checks - failures, checks);
    return failures;
}
