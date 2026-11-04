// GFX-initiated RDMA against rocm-ernic, end to end.
//
//   CPU    builds the WQE (ibv_post_send). ionic_dv_qp_set_gda() stops the
//          provider ringing, so no doorbell leaves the CPU.
//   GFX    writes the doorbell. This store starts the IO.
//   ernic  sees it, performs the RDMA WRITE, posts the CQE.
//   GFX    polls the CQ and sees the completion.
//
// The doorbell goes to a PROXY PAGE IN GUEST RAM, not to BAR2. A GFX kernel
// cannot reach BAR2: QEMU maps a vfio-user device's BAR as a ram_device
// region and never offers ram_device windows to peer vfio-user devices, so
// rocjitsu has no mapping and the store is dropped silently. Guest RAM it
// does share. The device DMA-polls the proxy page; the guest publishes that
// page's physical address through the one BAR2 slot the CPU can still write.
//
// Needs root: /proc/self/pagemap for the physical address, and the sysfs PCI
// resource to reach the control slot.
#include <hip/hip_runtime.h>
#include <infiniband/verbs.h>
#include <infiniband/ionic_dv.h>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <cerrno>
#include <cstdint>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>

#define IONIC_BAR2_DB_SIZE        ((size_t)4 * 1024 * 1024)
#define IONIC_DB_PAGE_SIZE        4096
#define IONIC_PROXY_DB_CTL_OFFSET (IONIC_BAR2_DB_SIZE - IONIC_DB_PAGE_SIZE)

#define CHK(x) do { if (!(x)) { \
    fprintf(stderr, "FAIL %s:%d: %s (%s)\n", __FILE__, __LINE__, #x, strerror(errno)); \
    exit(1); } } while (0)
#define HIP(x) do { hipError_t _e = (x); if (_e != hipSuccess) { \
    fprintf(stderr, "FAIL %s:%d: %s -> %s\n", __FILE__, __LINE__, #x, \
            hipGetErrorString(_e)); exit(1); } } while (0)

__global__ void ring_doorbell(volatile uint64_t *db, uint64_t val)
{
    if (threadIdx.x == 0 && blockIdx.x == 0) {
        *db = val;
        __threadfence_system();
    }
}

__global__ void poll_cqe(volatile uint64_t *cqe, uint64_t b0, uint64_t b1,
                         uint64_t *out, uint64_t spins)
{
    if (threadIdx.x || blockIdx.x) return;
    for (uint64_t i = 0; i < spins; i++) {
        uint64_t a = cqe[0], b = cqe[1];
        if (a != b0 || b != b1) { out[0]=a; out[1]=b; out[2]=i; out[3]=1; return; }
    }
    out[0]=cqe[0]; out[1]=cqe[1]; out[2]=spins; out[3]=0;
}

static uint64_t virt_to_phys(void *p)
{
    int fd = open("/proc/self/pagemap", O_RDONLY);
    if (fd < 0) return 0;
    uint64_t v = (uint64_t)(uintptr_t)p, e = 0, phys = 0;
    if (pread(fd, &e, 8, (off_t)((v / 4096) * 8)) == 8 && (e & (1ULL << 63)))
        phys = ((e & ((1ULL << 55) - 1)) * 4096) + (v % 4096);
    close(fd);
    return phys;
}

int main(int argc, char **argv)
{
    const char *devname = argc > 1 ? argv[1] : "rocep1s0";
    const char *pcislot = argc > 2 ? argv[2] : "0000:01:00.0";

    int n = 0;
    struct ibv_device **list = ibv_get_device_list(&n); CHK(list && n);
    struct ibv_device *dev = nullptr;
    for (int i = 0; i < n; i++)
        if (!strcmp(ibv_get_device_name(list[i]), devname)) dev = list[i];
    CHK(dev);
    printf("device: %s  (pci %s)\n", devname, pcislot);

    struct ibv_context *ctx = ibv_open_device(dev); CHK(ctx);
    struct ibv_pd *pd = ibv_alloc_pd(ctx); CHK(pd);
    struct ibv_port_attr pa; CHK(ibv_query_port(ctx, 1, &pa) == 0);
    union ibv_gid gid; CHK(ibv_query_gid(ctx, 1, 0, &gid) == 0);

    size_t len = 8192;
    char *buf = (char *)aligned_alloc(4096, len); CHK(buf);
    memset(buf, 0, len);
    memcpy(buf, "GFX-INITIATED-RDMA-PAYLOAD-0123456789", 38);
    struct ibv_mr *mr = ibv_reg_mr(pd, buf, len,
        IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE | IBV_ACCESS_REMOTE_READ);
    CHK(mr);

    struct ibv_cq *cq = ibv_create_cq(ctx, 64, nullptr, nullptr, 0); CHK(cq);
    struct ibv_qp_init_attr ia; memset(&ia, 0, sizeof ia);
    ia.send_cq = cq; ia.recv_cq = cq; ia.qp_type = IBV_QPT_RC; ia.sq_sig_all = 1;
    ia.cap.max_send_wr = 16; ia.cap.max_recv_wr = 16;
    ia.cap.max_send_sge = 1; ia.cap.max_recv_sge = 1;
    struct ibv_qp *qp = ibv_create_qp(pd, &ia); CHK(qp);

    struct ibv_qp_attr at;
    memset(&at, 0, sizeof at);
    at.qp_state = IBV_QPS_INIT; at.port_num = 1; at.pkey_index = 0;
    at.qp_access_flags = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE |
                         IBV_ACCESS_REMOTE_READ;
    CHK(ibv_modify_qp(qp, &at, IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT |
                      IBV_QP_ACCESS_FLAGS) == 0);
    memset(&at, 0, sizeof at);
    at.qp_state = IBV_QPS_RTR; at.path_mtu = pa.active_mtu;
    at.dest_qp_num = qp->qp_num; at.rq_psn = 0;
    at.max_dest_rd_atomic = 1; at.min_rnr_timer = 12;
    at.ah_attr.is_global = 1; at.ah_attr.grh.dgid = gid;
    at.ah_attr.grh.sgid_index = 0; at.ah_attr.grh.hop_limit = 64;
    at.ah_attr.dlid = pa.lid; at.ah_attr.port_num = 1;
    CHK(ibv_modify_qp(qp, &at, IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU |
            IBV_QP_DEST_QPN | IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC |
            IBV_QP_MIN_RNR_TIMER) == 0);
    memset(&at, 0, sizeof at);
    at.qp_state = IBV_QPS_RTS; at.timeout = 14; at.retry_cnt = 7;
    at.rnr_retry = 7; at.sq_psn = 0; at.max_rd_atomic = 1;
    CHK(ibv_modify_qp(qp, &at, IBV_QP_STATE | IBV_QP_TIMEOUT | IBV_QP_RETRY_CNT |
            IBV_QP_RNR_RETRY | IBV_QP_SQ_PSN | IBV_QP_MAX_QP_RD_ATOMIC) == 0);
    printf("qp 0x%x RTS\n", qp->qp_num);

    // ---- direct verbs -------------------------------------------------
    struct ionic_dv_ctx dvctx; memset(&dvctx, 0, sizeof dvctx);
    CHK(ionic_dv_get_ctx(&dvctx, ctx) == 0);
    struct ionic_dv_qp dvqp; memset(&dvqp, 0, sizeof dvqp);
    CHK(ionic_dv_get_qp(&dvqp, qp) == 0);
    struct ionic_dv_cq dvcq; memset(&dvcq, 0, sizeof dvcq);
    CHK(ionic_dv_get_cq(&dvcq, cq, 0) == 0);
    printf("DV: sq_qtype=%u sq.db_val=0x%lx cq.ptr=%p\n",
           dvctx.sq_qtype, dvqp.sq.db_val, dvcq.q.ptr);
    CHK(ionic_dv_qp_set_gda(qp, true, false) == 0);

    // ---- proxy doorbell page in guest RAM -----------------------------
    void *proxy = aligned_alloc(4096, 4096); CHK(proxy);
    memset(proxy, 0, 4096);
    CHK(mlock(proxy, 4096) == 0);
    uint64_t proxy_gpa = virt_to_phys(proxy);
    CHK(proxy_gpa != 0);
    printf("proxy page: va=%p gpa=0x%lx\n", proxy, proxy_gpa);

    char respath[256];
    snprintf(respath, sizeof respath,
             "/sys/bus/pci/devices/%s/resource2", pcislot);
    int rfd = open(respath, O_RDWR | O_SYNC);
    if (rfd < 0) { fprintf(stderr, "open %s: %s\n", respath, strerror(errno)); exit(1); }
    void *ctlmap = mmap(nullptr, IONIC_DB_PAGE_SIZE, PROT_READ | PROT_WRITE,
                        MAP_SHARED, rfd, (off_t)IONIC_PROXY_DB_CTL_OFFSET);
    if (ctlmap == MAP_FAILED) { perror("mmap resource2"); exit(1); }
    volatile uint64_t *ctl = (volatile uint64_t *)ctlmap;
    *ctl = proxy_gpa;
    __sync_synchronize();
    printf("published proxy gpa through BAR2 ctl slot (+0x%lx)\n",
           (unsigned long)IONIC_PROXY_DB_CTL_OFFSET);

    HIP(hipHostRegister(proxy, 4096, hipHostRegisterMapped));
    void *proxy_dev = nullptr;
    HIP(hipHostGetDevicePointer(&proxy_dev, proxy, 0));
    volatile uint64_t *db_dev =
        (volatile uint64_t *)((uintptr_t)proxy_dev + dvctx.sq_qtype * 8);
    printf("proxy mapped for GFX: dev=%p slot=%u\n", proxy_dev, dvctx.sq_qtype);

    void *cq_base = (void *)((uintptr_t)dvcq.q.ptr & ~0xfffUL);
    size_t cq_span = ((dvcq.q.size + ((uintptr_t)dvcq.q.ptr - (uintptr_t)cq_base))
                      + 4095) & ~4095UL;
    HIP(hipHostRegister(cq_base, cq_span, hipHostRegisterMapped));
    void *cq_dev_base = nullptr;
    HIP(hipHostGetDevicePointer(&cq_dev_base, cq_base, 0));
    volatile uint64_t *cqe_dev = (volatile uint64_t *)((uintptr_t)cq_dev_base +
                              ((uintptr_t)dvcq.q.ptr - (uintptr_t)cq_base));

    // ---- CPU builds the WQE; nothing is rung ---------------------------
    volatile uint64_t *cqe_host = (volatile uint64_t *)dvcq.q.ptr;
    uint64_t cq0 = cqe_host[0], cq1 = cqe_host[1];
    uint64_t *dbslot_host = &dvctx.db_ptr[dvctx.sq_qtype];
    uint64_t db_before = *dbslot_host;

    struct ibv_sge sge = { (uintptr_t)buf, 64, mr->lkey };
    struct ibv_send_wr wr, *bad = nullptr;
    memset(&wr, 0, sizeof wr);
    wr.wr_id = 0x6678; wr.sg_list = &sge; wr.num_sge = 1;
    wr.opcode = IBV_WR_RDMA_WRITE; wr.send_flags = IBV_SEND_SIGNALED;
    wr.wr.rdma.remote_addr = (uintptr_t)(buf + 4096); wr.wr.rdma.rkey = mr->rkey;
    int rc = ibv_post_send(qp, &wr, &bad);
    if (rc) { fprintf(stderr, "post_send: %s\n", strerror(rc)); exit(1); }
    printf("post_send: BAR2 doorbell 0x%lx -> 0x%lx (%s)\n",
           db_before, *dbslot_host,
           db_before == *dbslot_host ? "UNRUNG, as GDA intends" : "CPU RANG IT");

    // ---- GFX rings, into guest RAM ------------------------------------
    uint64_t ring_val = dvqp.sq.db_val | 1ULL;
    uint64_t *out_d = nullptr, out_h[4] = {0,0,0,0};
    HIP(hipMalloc(&out_d, sizeof out_h));
    HIP(hipMemset(out_d, 0, sizeof out_h));
    printf("GFX writing doorbell 0x%lx into the proxy page\n", ring_val);
    hipLaunchKernelGGL(ring_doorbell, dim3(1), dim3(1), 0, 0, db_dev, ring_val);
    HIP(hipDeviceSynchronize());
    printf("proxy slot after GFX store: 0x%lx (%s)\n",
           ((volatile uint64_t *)proxy)[dvctx.sq_qtype],
           ((volatile uint64_t *)proxy)[dvctx.sq_qtype] == ring_val
               ? "GPU STORE LANDED" : "LOST");

    /* Ring and poll are two kernels, not one. A single kernel that rings and
     * then spins is the tidier shape and is unusable here: this GPU is
     * emulated by rocjitsu, so every spin iteration costs real wall-clock
     * and the loop outlives any sensible timeout. Two kernels also keep the
     * claim honest -- the poll kernel reads the CQ from memory after the
     * device wrote it, which is what "the GPU saw the completion" means. */
    hipLaunchKernelGGL(poll_cqe, dim3(1), dim3(1), 0, 0, cqe_dev, cq0, cq1,
                       out_d, (uint64_t)1000000);
    HIP(hipDeviceSynchronize());
    HIP(hipMemcpy(out_h, out_d, sizeof out_h, hipMemcpyDeviceToHost));
    printf("GFX poll:  %s after %lu spins (cqe[0]=0x%lx)\n",
           out_h[3] ? "CQE SEEN" : "TIMEOUT", out_h[2], out_h[0]);

    bool data_ok = memcmp(buf, buf + 4096, 64) == 0;
    printf("payload at +4096: \"%.24s\"  %s\n", buf + 4096,
           data_ok ? "MATCH" : "MISMATCH");

    *ctl = 0;   // unpublish; the page goes away with this process
    __sync_synchronize();

    bool ok = out_h[3] && data_ok;
    printf("\n=== %s ===\n", ok
           ? "GFX-INITIATED RDMA: END TO END -- GPU rang, ernic completed, GPU saw it"
           : "incomplete -- see stages above");
    return ok ? 0 : 1;
}
