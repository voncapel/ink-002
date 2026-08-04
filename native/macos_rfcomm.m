#import <Foundation/Foundation.h>
#import <IOBluetooth/IOBluetooth.h>

#include <signal.h>
#include <unistd.h>

static const uint8_t kFirmwareQuery[] = {0x64, 0x11, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x9b};
static const uint8_t kSerialQuery[] = {0x64, 0x12, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x9b};
static const uint8_t kStatusQuery[] = {0x64, 0x10, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x9b};
static const uint8_t kCancelQuery[] = {0x64, 0x52, 0x02, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x9b};
static volatile sig_atomic_t gCancelRequested = 0;

static void HandleCancelSignal(int signalNumber) {
    (void)signalNumber;
    gCancelRequested = 1;
}

@interface S002Delegate : NSObject <IOBluetoothRFCOMMChannelDelegate>
@property(nonatomic, strong) NSMutableData *replies;
@property(nonatomic) BOOL closed;
@end

@implementation S002Delegate
- (instancetype)init {
    self = [super init];
    if (self) {
        _replies = [NSMutableData data];
        _closed = NO;
    }
    return self;
}

- (void)rfcommChannelData:(IOBluetoothRFCOMMChannel *)channel
                     data:(void *)dataPointer
                   length:(size_t)dataLength {
    [self.replies appendBytes:dataPointer length:dataLength];
}

- (void)rfcommChannelClosed:(IOBluetoothRFCOMMChannel *)channel {
    self.closed = YES;
}
@end

static void PumpRunLoop(NSTimeInterval seconds) {
    [[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:seconds]];
}

static BOOL WriteBytes(IOBluetoothRFCOMMChannel *channel,
                       const uint8_t *bytes,
                       NSUInteger length,
                       NSUInteger requestedChunk,
                       useconds_t delayMicroseconds,
                       NSString **errorMessage) {
    NSUInteger mtu = (NSUInteger)[channel getMTU];
    NSUInteger chunkSize = requestedChunk > 0
        ? MIN(requestedChunk, mtu > 0 ? mtu : requestedChunk)
        : mtu;
    chunkSize = MIN(chunkSize, (NSUInteger)UINT16_MAX);
    if (chunkSize == 0) {
        chunkSize = 64;
    }

    for (NSUInteger offset = 0; offset < length; offset += chunkSize) {
        if (gCancelRequested) {
            return YES;
        }
        if (![channel isOpen]) {
            *errorMessage = [NSString stringWithFormat:@"RFCOMM channel closed after %lu/%lu bytes",
                              (unsigned long)offset, (unsigned long)length];
            return NO;
        }
        UInt16 count = (UInt16)MIN(chunkSize, length - offset);
        IOReturn result = [channel writeSync:(void *)(bytes + offset) length:count];
        if (result != kIOReturnSuccess) {
            *errorMessage = [NSString stringWithFormat:@"RFCOMM write failed after %lu/%lu bytes (0x%08x)",
                              (unsigned long)offset, (unsigned long)length, result];
            return NO;
        }
        if (delayMicroseconds > 0) {
            usleep(delayMicroseconds);
        }
    }
    return YES;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 6) {
            fprintf(stderr, "usage: s002-rfcomm MAC CHANNEL KEEPALIVE_SECONDS CHUNK_SIZE CHUNK_DELAY_SECONDS\n");
            return 64;
        }

        NSString *address = [NSString stringWithUTF8String:argv[1]];
        BluetoothRFCOMMChannelID channelID = (BluetoothRFCOMMChannelID)strtoul(argv[2], NULL, 10);
        NSTimeInterval keepaliveSeconds = strtod(argv[3], NULL);
        NSUInteger chunkSize = (NSUInteger)strtoul(argv[4], NULL, 10);
        useconds_t chunkDelay = (useconds_t)(strtod(argv[5], NULL) * 1000000.0);
        NSData *payload = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        if (payload.length == 0) {
            fprintf(stderr, "empty S002 payload\n");
            return 65;
        }
        signal(SIGTERM, HandleCancelSignal);
        signal(SIGINT, HandleCancelSignal);

        IOBluetoothDevice *device = [IOBluetoothDevice deviceWithAddressString:address];
        if (device == nil) {
            fprintf(stderr, "invalid Bluetooth address: %s\n", argv[1]);
            return 66;
        }

        S002Delegate *delegate = [[S002Delegate alloc] init];
        IOBluetoothRFCOMMChannel *channel = nil;
        IOReturn opened = [device openRFCOMMChannelSync:&channel
                                          withChannelID:channelID
                                               delegate:delegate];
        if (opened != kIOReturnSuccess || channel == nil) {
            fprintf(stderr, "could not open RFCOMM channel %u (0x%08x)\n", channelID, opened);
            return 67;
        }

        NSString *error = nil;
        BOOL ok = WriteBytes(channel, kFirmwareQuery, sizeof(kFirmwareQuery), chunkSize, 0, &error);
        if (ok && !gCancelRequested) {
            usleep(100000);
            ok = WriteBytes(channel, kSerialQuery, sizeof(kSerialQuery), chunkSize, 0, &error);
        }
        if (ok && !gCancelRequested) {
            usleep(200000);
            ok = WriteBytes(channel, payload.bytes, payload.length, chunkSize, chunkDelay, &error);
        }

        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:keepaliveSeconds];
        while (ok && !gCancelRequested && !delegate.closed && [deadline timeIntervalSinceNow] > 0) {
            ok = WriteBytes(channel, kStatusQuery, sizeof(kStatusQuery), chunkSize, 0, &error);
            PumpRunLoop(0.15);
        }
        if (gCancelRequested) {
            gCancelRequested = 0;
            NSString *cancelError = nil;
            WriteBytes(channel, kCancelQuery, sizeof(kCancelQuery), chunkSize, 0, &cancelError);
            PumpRunLoop(0.12);
            [channel closeChannel];
            return 70;
        }
        PumpRunLoop(0.1);
        [channel closeChannel];

        if (!ok) {
            fprintf(stderr, "%s\n", error.UTF8String);
            return 68;
        }
        if (delegate.closed) {
            fprintf(stderr, "printer closed RFCOMM before the job completed\n");
            return 69;
        }

        [[NSFileHandle fileHandleWithStandardOutput] writeData:delegate.replies];
        return 0;
    }
}
