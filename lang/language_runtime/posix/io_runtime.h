#ifndef DRIFT_IO_RUNTIME_H
#define DRIFT_IO_RUNTIME_H

#include <stdint.h>

#include "string_runtime.h"

int64_t drift_io_open(DriftString path, int64_t flags, int64_t mode);
int64_t drift_io_close(int64_t fd);
int64_t drift_io_read(int64_t fd, void *buf, int64_t len);
int64_t drift_io_write(int64_t fd, const void *buf, int64_t len);
int64_t drift_io_errno(void);
int64_t drift_io_set_nonblocking(int64_t fd);
int64_t drift_net_listen(DriftString *ip, int64_t port);
int64_t drift_net_accept(int64_t fd);
int64_t drift_net_connect(DriftString *ip, int64_t port, int64_t deadline_ms);
int64_t drift_net_listener_port(int64_t fd);
int64_t drift_net_udp_local_port(int64_t fd);
int64_t drift_net_udp_bind(DriftString *ip, int64_t port);
int64_t drift_net_udp_bind_v6(DriftString *ip, int64_t port);
int64_t drift_net_udp_send_to(int64_t fd, DriftString *ip, int64_t port, const void *buf, int64_t len);
int64_t drift_net_udp_send_to_v6(int64_t fd, DriftString *ip, int64_t port, const void *buf, int64_t len);
int64_t drift_net_udp_recv_from(int64_t fd, void *buf, int64_t len, DriftString *out_ip, int64_t *out_port);
int64_t drift_net_udp_recv_from_v6(int64_t fd, void *buf, int64_t len, DriftString *out_ip, int64_t *out_port);
int64_t drift_net_set_nodelay(int64_t fd, int64_t enabled);
int64_t drift_net_get_nodelay(int64_t fd);

#endif  // DRIFT_IO_RUNTIME_H
