#include "io_runtime.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <string.h>

static int drift_set_nonblocking(int fd);

int64_t drift_io_open(DriftString path, int64_t flags, int64_t mode) {
	char *cstr = drift_string_to_cstr(path);
	int fd = open(cstr, (int)flags, (mode_t)mode);
	int err = errno;
	free(cstr);
	errno = err;
	return (int64_t)fd;
}

extern void drift_reactor_forget_fd(int fd);

int64_t drift_io_close(int64_t fd) {
	/* Remove the persistent reactor watch BEFORE close().  Reverse order
	 * would be unsafe: close() frees the fd number, another thread can
	 * immediately recycle it via accept()/socket()/open(), and a late
	 * forget_fd() would destroy the new connection's watch.
	 *
	 * If close() subsequently fails:
	 *   EBADF  — fd was already invalid; no watch should exist anyway.
	 *   EINTR  — on Linux, close() consumes the fd even on EINTR (the
	 *            fd is gone regardless), so freeing the watch is correct.
	 * In both cases register_io() on a later EAGAIN would malloc a fresh
	 * watch and EPOLL_CTL_ADD, degrading to the old per-op path for that
	 * one operation — safe, just slightly slower. */
	drift_reactor_forget_fd((int)fd);
	int rc = close((int)fd);
	return (int64_t)rc;
}

int64_t drift_io_read(int64_t fd, void *buf, int64_t len) {
	ssize_t n = read((int)fd, buf, (size_t)len);
	return (int64_t)n;
}

int64_t drift_io_write(int64_t fd, const void *buf, int64_t len) {
	ssize_t n = write((int)fd, buf, (size_t)len);
	return (int64_t)n;
}

int64_t drift_io_errno(void) {
	return (int64_t)errno;
}

int64_t drift_io_set_nonblocking(int64_t fd) {
	return (int64_t)drift_set_nonblocking((int)fd);
}

static int drift_set_nonblocking(int fd) {
	int flags = fcntl(fd, F_GETFL, 0);
	if (flags < 0) {
		return -1;
	}
	if (fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) {
		return -1;
	}
	return 0;
}

int64_t drift_net_listen(DriftString *ip, int64_t port) {
	int fd = socket(AF_INET, SOCK_STREAM, 0);
	if (fd < 0) {
		return -1;
	}
	int reuse = 1;
	(void)setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
	if (drift_set_nonblocking(fd) < 0) {
		int err = errno;
		close(fd);
		errno = err;
		return -1;
	}
	char *ip_cstr = drift_string_to_cstr(*ip);
	struct sockaddr_in addr;
	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_port = htons((uint16_t)port);
	if (inet_pton(AF_INET, ip_cstr, &addr.sin_addr) <= 0) {
		free(ip_cstr);
		close(fd);
		errno = EINVAL;
		return -1;
	}
	free(ip_cstr);
	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		int err = errno;
		close(fd);
		errno = err;
		return -1;
	}
	if (listen(fd, 128) < 0) {
		int err = errno;
		close(fd);
		errno = err;
		return -1;
	}
	return fd;
}

int64_t drift_net_accept(int64_t fd) {
	int client_fd = accept((int)fd, NULL, NULL);
	if (client_fd < 0) {
		return -1;
	}
	if (drift_set_nonblocking(client_fd) < 0) {
		int err = errno;
		close(client_fd);
		errno = err;
		return -1;
	}
	return client_fd;
}

int64_t drift_net_listener_port(int64_t fd) {
	struct sockaddr_in addr;
	socklen_t len = sizeof(addr);
	if (getsockname((int)fd, (struct sockaddr *)&addr, &len) < 0) {
		return -1;
	}
	return (int64_t)ntohs(addr.sin_port);
}

int64_t drift_net_udp_local_port(int64_t fd) {
	struct sockaddr_in addr;
	socklen_t len = sizeof(addr);
	if (getsockname((int)fd, (struct sockaddr *)&addr, &len) < 0) {
		return -1;
	}
	return (int64_t)ntohs(addr.sin_port);
}

int64_t drift_net_udp_bind(DriftString *ip, int64_t port) {
	int fd = socket(AF_INET, SOCK_DGRAM, 0);
	if (fd < 0) {
		return -1;
	}
	if (drift_set_nonblocking(fd) < 0) {
		int err = errno;
		close(fd);
		errno = err;
		return -1;
	}
	char *ip_cstr = drift_string_to_cstr(*ip);
	struct sockaddr_in addr;
	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_port = htons((uint16_t)port);
	if (inet_pton(AF_INET, ip_cstr, &addr.sin_addr) <= 0) {
		free(ip_cstr);
		close(fd);
		errno = EINVAL;
		return -1;
	}
	free(ip_cstr);
	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		int err = errno;
		close(fd);
		errno = err;
		return -1;
	}
	return fd;
}

int64_t drift_net_udp_bind_v6(DriftString *ip, int64_t port) {
	int fd = socket(AF_INET6, SOCK_DGRAM, 0);
	if (fd < 0) {
		return -1;
	}
	if (drift_set_nonblocking(fd) < 0) {
		int err = errno;
		close(fd);
		errno = err;
		return -1;
	}
	char *ip_cstr = drift_string_to_cstr(*ip);
	struct sockaddr_in6 addr;
	memset(&addr, 0, sizeof(addr));
	addr.sin6_family = AF_INET6;
	addr.sin6_port = htons((uint16_t)port);
	if (inet_pton(AF_INET6, ip_cstr, &addr.sin6_addr) <= 0) {
		free(ip_cstr);
		close(fd);
		errno = EINVAL;
		return -1;
	}
	free(ip_cstr);
	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		int err = errno;
		close(fd);
		errno = err;
		return -1;
	}
	return fd;
}

int64_t drift_net_udp_send_to(int64_t fd, DriftString *ip, int64_t port, const void *buf, int64_t len) {
	char *ip_cstr = drift_string_to_cstr(*ip);
	struct sockaddr_in addr;
	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_port = htons((uint16_t)port);
	if (inet_pton(AF_INET, ip_cstr, &addr.sin_addr) <= 0) {
		free(ip_cstr);
		errno = EINVAL;
		return -1;
	}
	free(ip_cstr);
	ssize_t n = sendto((int)fd, buf, (size_t)len, MSG_NOSIGNAL, (struct sockaddr *)&addr, sizeof(addr));
	return (int64_t)n;
}

int64_t drift_net_udp_send_to_v6(int64_t fd, DriftString *ip, int64_t port, const void *buf, int64_t len) {
	char *ip_cstr = drift_string_to_cstr(*ip);
	struct sockaddr_in6 addr;
	memset(&addr, 0, sizeof(addr));
	addr.sin6_family = AF_INET6;
	addr.sin6_port = htons((uint16_t)port);
	if (inet_pton(AF_INET6, ip_cstr, &addr.sin6_addr) <= 0) {
		free(ip_cstr);
		errno = EINVAL;
		return -1;
	}
	free(ip_cstr);
	ssize_t n = sendto((int)fd, buf, (size_t)len, MSG_NOSIGNAL, (struct sockaddr *)&addr, sizeof(addr));
	return (int64_t)n;
}

int64_t drift_net_udp_recv_from(int64_t fd, void *buf, int64_t len, DriftString *out_ip, int64_t *out_port) {
	struct sockaddr_in addr;
	socklen_t addr_len = sizeof(addr);
	ssize_t n = recvfrom((int)fd, buf, (size_t)len, 0, (struct sockaddr *)&addr, &addr_len);
	if (n < 0) {
		return -1;
	}
	char ip_buf[INET_ADDRSTRLEN];
	const char *ip_str = inet_ntop(AF_INET, &addr.sin_addr, ip_buf, sizeof(ip_buf));
	if (ip_str != NULL) {
		*out_ip = drift_string_from_cstr(ip_str);
	}
	*out_port = (int64_t)ntohs(addr.sin_port);
	return (int64_t)n;
}

int64_t drift_net_udp_recv_from_v6(int64_t fd, void *buf, int64_t len, DriftString *out_ip, int64_t *out_port) {
	struct sockaddr_in6 addr;
	socklen_t addr_len = sizeof(addr);
	ssize_t n = recvfrom((int)fd, buf, (size_t)len, 0, (struct sockaddr *)&addr, &addr_len);
	if (n < 0) {
		return -1;
	}
	char ip_buf[INET6_ADDRSTRLEN];
	const char *ip_str = inet_ntop(AF_INET6, &addr.sin6_addr, ip_buf, sizeof(ip_buf));
	if (ip_str != NULL) {
		*out_ip = drift_string_from_cstr(ip_str);
	}
	*out_port = (int64_t)ntohs(addr.sin6_port);
	return (int64_t)n;
}

int64_t drift_net_set_nodelay(int64_t fd, int64_t enabled) {
	int val = (int)enabled;
	if (setsockopt((int)fd, IPPROTO_TCP, TCP_NODELAY, &val, sizeof(val)) < 0)
		return -1;
	return 0;
}

int64_t drift_net_get_nodelay(int64_t fd) {
	int val = 0;
	socklen_t len = sizeof(val);
	if (getsockopt((int)fd, IPPROTO_TCP, TCP_NODELAY, &val, &len) < 0)
		return -1;
	return (int64_t)val;
}
