#include <stdatomic.h>
#include <stdint.h>

static memory_order drift_order_load(int64_t order) {
	switch (order) {
	case 1: return memory_order_acquire;
	case 4: return memory_order_seq_cst;
	case 2: return memory_order_acquire;
	case 3: return memory_order_acquire;
	default: return memory_order_relaxed;
	}
}

static memory_order drift_order_store(int64_t order) {
	switch (order) {
	case 2: return memory_order_release;
	case 4: return memory_order_seq_cst;
	case 1: return memory_order_release;
	case 3: return memory_order_release;
	default: return memory_order_relaxed;
	}
}

static memory_order drift_order_rmw(int64_t order) {
	switch (order) {
	case 1: return memory_order_acquire;
	case 2: return memory_order_release;
	case 3: return memory_order_acq_rel;
	case 4: return memory_order_seq_cst;
	default: return memory_order_relaxed;
	}
}

uint8_t drift_atomic_load_bool(uint8_t *p, int64_t order) {
	_Atomic uint8_t *ap = (_Atomic uint8_t *)p;
	return atomic_load_explicit(ap, drift_order_load(order));
}

void drift_atomic_store_bool(uint8_t *p, uint8_t v, int64_t order) {
	_Atomic uint8_t *ap = (_Atomic uint8_t *)p;
	atomic_store_explicit(ap, v, drift_order_store(order));
}

int64_t drift_atomic_load_int(int64_t *p, int64_t order) {
	_Atomic int64_t *ap = (_Atomic int64_t *)p;
	return atomic_load_explicit(ap, drift_order_load(order));
}

void drift_atomic_store_int(int64_t *p, int64_t v, int64_t order) {
	_Atomic int64_t *ap = (_Atomic int64_t *)p;
	atomic_store_explicit(ap, v, drift_order_store(order));
}

int64_t drift_atomic_fetch_add_int(int64_t *p, int64_t v, int64_t order) {
	_Atomic int64_t *ap = (_Atomic int64_t *)p;
	return atomic_fetch_add_explicit(ap, v, drift_order_rmw(order));
}

uint64_t drift_atomic_load_uint(uint64_t *p, int64_t order) {
	_Atomic uint64_t *ap = (_Atomic uint64_t *)p;
	return atomic_load_explicit(ap, drift_order_load(order));
}

void drift_atomic_store_uint(uint64_t *p, uint64_t v, int64_t order) {
	_Atomic uint64_t *ap = (_Atomic uint64_t *)p;
	atomic_store_explicit(ap, v, drift_order_store(order));
}

uint64_t drift_atomic_fetch_add_uint(uint64_t *p, uint64_t v, int64_t order) {
	_Atomic uint64_t *ap = (_Atomic uint64_t *)p;
	return atomic_fetch_add_explicit(ap, v, drift_order_rmw(order));
}

uint64_t drift_atomic_load_uint64(uint64_t *p, int64_t order) {
	_Atomic uint64_t *ap = (_Atomic uint64_t *)p;
	return atomic_load_explicit(ap, drift_order_load(order));
}

void drift_atomic_store_uint64(uint64_t *p, uint64_t v, int64_t order) {
	_Atomic uint64_t *ap = (_Atomic uint64_t *)p;
	atomic_store_explicit(ap, v, drift_order_store(order));
}

uint64_t drift_atomic_fetch_add_uint64(uint64_t *p, uint64_t v, int64_t order) {
	_Atomic uint64_t *ap = (_Atomic uint64_t *)p;
	return atomic_fetch_add_explicit(ap, v, drift_order_rmw(order));
}
