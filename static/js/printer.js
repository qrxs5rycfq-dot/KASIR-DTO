/**
 * Global Printer Manager
 * Handles Bluetooth printer connection across all pages
 * Auto-reconnects on page load if printer was previously connected
 * Includes pending print queue for receipts when printer is disconnected
 */

const PrinterManager = {
    device: null,
    characteristic: null,
    isConnected: false,
    printerName: null,
    printerId: null,  // MAC address / device ID for reliable reconnection
    reconnectAttempts: 0,
    maxReconnectAttempts: 10,
    reconnectInterval: null,
    pendingPrintQueue: [],  // Queue for pending receipts when printer disconnected
    isProcessingQueue: false,
    lastDisconnectTime: 0,  // Timestamp of last disconnect for cooldown
    disconnectCount: 0,  // Count of disconnects for exponential backoff
    autoReconnectSupported: true,  // Flag to track if auto-reconnect is supported
    lastFocusReconnectTime: 0,  // Timestamp of last focus reconnect for debounce
    
    // Configuration constants
    CONFIG: {
        BASE_RETRY_DELAY_MS: 500,       // Base delay between retries
        FOCUS_DEBOUNCE_MS: 2000,        // Minimum time between focus reconnect attempts
        INIT_RECONNECT_DELAY_MS: 1000,  // Delay before retry after page load
        WATCH_TIMEOUT_MS: 8000,         // Timeout for watching advertisements
        BACKGROUND_CHECK_INTERVAL_MS: 5000,  // Interval for background reconnect checker
    },
    
    // ESC/POS Commands for thermal printers
    ESC_POS: {
        INIT: [0x1B, 0x40],
        ALIGN_CENTER: [0x1B, 0x61, 0x01],
        ALIGN_LEFT: [0x1B, 0x61, 0x00],
        BOLD_ON: [0x1B, 0x45, 0x01],
        BOLD_OFF: [0x1B, 0x45, 0x00],
        DOUBLE_HEIGHT: [0x1B, 0x21, 0x10],
        NORMAL_SIZE: [0x1B, 0x21, 0x00],
        CUT_PAPER: [0x1D, 0x56, 0x00],
        FEED_LINE: [0x0A],
        FEED_LINES: (n) => [0x1B, 0x64, n],
    },
    
    // Check if auto-reconnect is supported
    checkAutoReconnectSupport() {
        if (!navigator.bluetooth) {
            this.autoReconnectSupported = false;
            console.log('PrinterManager: Bluetooth not supported');
            return false;
        }
        if (!navigator.bluetooth.getDevices) {
            this.autoReconnectSupported = false;
            console.log('PrinterManager: getDevices() not supported - manual connection required');
            return false;
        }
        this.autoReconnectSupported = true;
        return true;
    },
    
    // Initialize printer manager
    async init() {
        console.log('PrinterManager: Initializing...');
        // Check if auto-reconnect is supported
        this.checkAutoReconnectSupport();
        // Load pending queue from localStorage
        this.loadPendingQueue();
        await this.loadSavedPrinter();
        this.updateStatusUI();
        
        // Start background reconnect checker only if supported
        if (this.autoReconnectSupported) {
            this.startReconnectChecker();
        }
        
        // Also try to reconnect immediately on init if we have saved printer
        if (this.autoReconnectSupported && (this.printerName || this.printerId) && !this.isConnected) {
            // Small delay to allow page to fully load
            setTimeout(() => {
                if (!this.isConnected) {
                    console.log('PrinterManager: Retrying auto-connect after page load...');
                    this.reconnectAttempts = 0;  // Reset attempts for fresh start
                    this.autoReconnect();
                }
            }, this.CONFIG.INIT_RECONNECT_DELAY_MS);
        }
    },
    
    // Load pending queue from server database only
    loadPendingQueue() {
        // Load from server database (the only source of truth)
        this.loadPendingFromServer();
    },
    
    // Load pending prints from server database
    async loadPendingFromServer() {
        try {
            const response = await fetch('/api/pending-prints');
            if (response.ok) {
                const data = await response.json();
                if (data.success && data.pending_prints) {
                    // Clear local queue and replace with server data
                    this.pendingPrintQueue = data.pending_prints.map(serverItem => ({
                        id: `server-${serverItem.id}`,
                        serverId: serverItem.id,
                        data: JSON.parse(serverItem.receipt_data),
                        addedAt: serverItem.created_at,
                        retryCount: serverItem.retry_count,
                        isServerItem: true
                    }));
                    console.log('PrinterManager: Loaded', data.pending_prints.length, 'pending receipts from database');
                }
            }
        } catch (error) {
            console.error('PrinterManager: Error loading pending queue from database:', error);
        }
        
        // Notify user if there are pending receipts
        const totalCount = this.pendingPrintQueue.length;
        if (totalCount > 0) {
            setTimeout(() => {
                this.showNotification(
                    `Ada ${totalCount} struk menunggu dicetak. Buka Printer Station untuk mencetak.`, 
                    'warning'
                );
            }, 2000);
        }
    },
    
    // Save pending queue - now only uses server database
    savePendingQueue() {
        // No longer using localStorage, database is managed via API calls
        console.log('PrinterManager: Queue saved to database via API');
    },
    
    // Add receipt to pending queue (database only)
    async addToPendingQueue(receiptData, orderId = null) {
        // Save to server database
        try {
            const response = await fetch('/api/pending-prints', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    order_id: orderId,
                    receipt_data: receiptData,
                    copies: 1,
                    current_copy: 1
                })
            });
            
            if (response.ok) {
                const data = await response.json();
                if (data.success) {
                    // Reload from database to sync local state
                    await this.loadPendingFromServer();
                    console.log('PrinterManager: Saved pending print to database, ID:', data.pending_print.id);
                    this.showNotification(`Struk ditambahkan ke antrian (${this.pendingPrintQueue.length} pending)`, 'warning');
                }
            }
        } catch (error) {
            console.error('PrinterManager: Error saving pending print to database:', error);
            this.showNotification('Gagal menyimpan struk ke antrian', 'error');
        }
    },
    
    // Get pending queue count from database
    getPendingCount() {
        return this.pendingPrintQueue.length;
    },
    
    // Get total pending count from database
    async getTotalPendingCount() {
        try {
            const response = await fetch('/api/pending-prints');
            if (response.ok) {
                const data = await response.json();
                if (data.success) {
                    return data.count || data.pending_prints.length;
                }
            }
        } catch (error) {
            console.error('PrinterManager: Error getting pending count from database:', error);
        }
        return this.pendingPrintQueue.length;
    },
    
    // Schedule pending queue processing with retry
    schedulePendingQueueProcessing() {
        console.log('PrinterManager: Scheduling pending queue processing...');
        
        // Reload pending prints from database
        setTimeout(async () => {
            if (this.isConnected && this.characteristic) {
                console.log('PrinterManager: Reloading pending prints from database...');
                await this.loadPendingFromServer();
            }
        }, 1000);
        
        // First attempt after 2 seconds (wait for connection to stabilize)
        setTimeout(async () => {
            if (this.isConnected && this.characteristic && this.pendingPrintQueue.length > 0) {
                console.log('PrinterManager: First attempt to process pending queue...');
                await this.processPendingQueue();
            }
        }, 2000);
        
        // Retry after 5 seconds if still have pending items
        setTimeout(async () => {
            await this.loadPendingFromServer();  // Reload in case new items added
            if (this.isConnected && this.characteristic && this.pendingPrintQueue.length > 0 && !this.isProcessingQueue) {
                console.log('PrinterManager: Retry processing pending queue...');
                await this.processPendingQueue();
            }
        }, 5000);
        
        // Final retry after 10 seconds
        setTimeout(async () => {
            await this.loadPendingFromServer();  // Reload in case new items added
            if (this.isConnected && this.characteristic && this.pendingPrintQueue.length > 0 && !this.isProcessingQueue) {
                console.log('PrinterManager: Final retry processing pending queue...');
                await this.processPendingQueue();
            }
        }, 10000);
    },
    
    // Process pending queue from database when printer is connected
    async processPendingQueue() {
        // Check all conditions before processing
        if (this.isProcessingQueue) {
            console.log('PrinterManager: Already processing queue, skipping...');
            return;
        }
        
        if (!this.isConnected || !this.characteristic) {
            console.log('PrinterManager: Printer not ready, skipping queue processing');
            return;
        }
        
        // Reload from database to get latest
        await this.loadPendingFromServer();
        
        if (this.pendingPrintQueue.length === 0) {
            console.log('PrinterManager: No pending receipts to process');
            return;
        }
        
        this.isProcessingQueue = true;
        console.log('PrinterManager: Processing', this.pendingPrintQueue.length, 'pending receipts from database');
        this.showNotification(`Mencetak ${this.pendingPrintQueue.length} struk pending...`, 'info');
        
        let successCount = 0;
        let failCount = 0;
        
        // Process items one by one
        for (const item of [...this.pendingPrintQueue]) {
            if (!this.isConnected || !this.characteristic) {
                console.log('PrinterManager: Printer disconnected during processing, stopping...');
                break;
            }
            
            try {
                console.log('PrinterManager: Printing pending receipt', item.id);
                await this.printRaw(item.data);
                
                // Mark as completed in database
                if (item.serverId) {
                    try {
                        await fetch(`/api/pending-prints/${item.serverId}/complete`, {
                            method: 'POST'
                        });
                        console.log('PrinterManager: Marked as completed in database:', item.serverId);
                    } catch (e) {
                        console.error('PrinterManager: Error marking as completed:', e);
                    }
                }
                
                successCount++;
                console.log('PrinterManager: Successfully printed pending receipt', item.id);
                
                // Delay between prints to prevent overlap
                await new Promise(resolve => setTimeout(resolve, 1500));
            } catch (error) {
                console.error('PrinterManager: Failed to print pending receipt:', error);
                
                // Update retry count in database
                if (item.serverId) {
                    try {
                        await fetch(`/api/pending-prints/${item.serverId}/fail`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ error_message: error.message })
                        });
                    } catch (e) {
                        console.error('PrinterManager: Error updating retry count:', e);
                    }
                }
                
                failCount++;
                
                // Wait before next item
                await new Promise(resolve => setTimeout(resolve, 2000));
            }
        }
        
        // Reload from database to sync local state
        await this.loadPendingFromServer();
        
        this.isProcessingQueue = false;
        
        if (successCount > 0) {
            this.showNotification(`${successCount} struk berhasil dicetak!`, 'success');
        }
        if (failCount > 0) {
            this.showNotification(`${failCount} struk gagal, akan dicoba lagi nanti`, 'warning');
        }
    },
    
    // Check if reconnect should be attempted
    shouldAttemptReconnect() {
        return !this.isConnected && 
               (this.printerName || this.printerId) && 
               this.reconnectAttempts < this.maxReconnectAttempts && 
               this.autoReconnectSupported;
    },
    
    // Start background reconnect checker
    startReconnectChecker() {
        // Don't start if auto-reconnect is not supported
        if (!this.autoReconnectSupported) {
            console.log('PrinterManager: Auto-reconnect not supported, skipping background checker');
            return;
        }
        
        // Clear existing interval
        if (this.reconnectInterval) {
            clearInterval(this.reconnectInterval);
        }
        
        // Check periodically if we need to reconnect
        this.reconnectInterval = setInterval(async () => {
            if (this.shouldAttemptReconnect()) {
                console.log('PrinterManager: Background reconnect attempt', this.reconnectAttempts + 1);
                await this.autoReconnect();
            }
        }, this.CONFIG.BACKGROUND_CHECK_INTERVAL_MS);
    },
    
    // Stop background reconnect checker
    stopReconnectChecker() {
        if (this.reconnectInterval) {
            clearInterval(this.reconnectInterval);
            this.reconnectInterval = null;
        }
    },
    
    // Load saved printer from database and attempt auto-reconnect
    async loadSavedPrinter() {
        try {
            const response = await fetch('/api/printer-status');
            if (response.ok) {
                const data = await response.json();
                if (data.printer_name || data.printer_id) {
                    this.printerName = data.printer_name;
                    this.printerId = data.printer_id;
                    console.log('PrinterManager: Found saved printer:', this.printerName, 'ID:', this.printerId);
                    // Attempt auto-reconnect immediately only if supported
                    if (this.autoReconnectSupported) {
                        this.updateStatusUI('connecting');
                        await this.autoReconnect();
                    } else {
                        // Show last known status with manual connect message
                        this.updateStatusUI('lastknown');
                    }
                }
            }
        } catch (error) {
            console.error('PrinterManager: Error loading saved printer:', error);
        }
    },
    
    // Auto-reconnect to previously paired device
    async autoReconnect() {
        // Early exit if auto-reconnect is not supported
        if (!this.autoReconnectSupported) {
            this.updateStatusUI('lastknown');
            return false;
        }
        
        if (!navigator.bluetooth) {
            console.log('PrinterManager: Bluetooth not supported');
            this.autoReconnectSupported = false;
            this.stopReconnectChecker();
            this.updateStatusUI('lastknown');
            return false;
        }
        
        // Check if getDevices is supported (Chrome 85+)
        if (!navigator.bluetooth.getDevices) {
            console.log('PrinterManager: getDevices() not supported - please click to connect manually');
            this.autoReconnectSupported = false;
            this.stopReconnectChecker();
            this.updateStatusUI('lastknown');
            return false;
        }
        
        try {
            this.reconnectAttempts++;
            this.updateStatusUI('connecting');
            
            // Get previously paired devices
            const devices = await navigator.bluetooth.getDevices();
            console.log('PrinterManager: Found', devices.length, 'paired devices');
            
            // Find our printer by ID (more reliable) or name
            let matchingDevice = null;
            if (this.printerId) {
                matchingDevice = devices.find(d => d.id === this.printerId);
            }
            if (!matchingDevice && this.printerName) {
                matchingDevice = devices.find(d => d.name === this.printerName);
            }
            
            if (matchingDevice) {
                console.log('PrinterManager: Found matching device:', matchingDevice.name, 'ID:', matchingDevice.id);
                
                // Try direct GATT connection with retry
                for (let attempt = 1; attempt <= 3; attempt++) {
                    try {
                        console.log(`PrinterManager: Connection attempt ${attempt}/3`);
                        const connected = await this.connectToDevice(matchingDevice);
                        if (connected) {
                            this.reconnectAttempts = 0; // Reset on successful connection
                            console.log('PrinterManager: Successfully connected!');
                            return true;
                        }
                    } catch (e) {
                        console.log(`PrinterManager: Attempt ${attempt} failed:`, e.message);
                        if (attempt < 3) {
                            // Wait before retry (increasing delay)
                            await new Promise(resolve => setTimeout(resolve, this.CONFIG.BASE_RETRY_DELAY_MS * attempt));
                        }
                    }
                }
                
                // Try watching for advertisements (device might be waking up)
                try {
                    console.log('PrinterManager: Trying to watch for device advertisements...');
                    const success = await this.watchAndConnect(matchingDevice);
                    if (success) {
                        this.reconnectAttempts = 0;
                        return true;
                    }
                } catch (e) {
                    console.log('PrinterManager: Watch advertisements failed:', e.message);
                }
            } else {
                console.log('PrinterManager: No matching device found in paired devices');
            }
            
            // Show last known status if reconnect failed
            if (!this.isConnected && (this.printerName || this.printerId)) {
                this.updateStatusUI('lastknown');
            }
            
            return false;
            
        } catch (error) {
            console.error('PrinterManager: Auto-reconnect error:', error);
            if (this.printerName || this.printerId) {
                this.updateStatusUI('lastknown');
            }
            return false;
        }
    },
    
    // Watch for device advertisements and connect when available
    async watchAndConnect(device) {
        return new Promise((resolve, reject) => {
            const abortController = new AbortController();
            const timeout = setTimeout(() => {
                abortController.abort();
                resolve(false);
            }, this.CONFIG.WATCH_TIMEOUT_MS);  // Configurable timeout
            
            device.addEventListener('advertisementreceived', async (event) => {
                console.log('PrinterManager: Advertisement received from', event.device.name);
                clearTimeout(timeout);
                abortController.abort();
                const connected = await this.connectToDevice(device);
                resolve(connected);
            }, { once: true });
            
            device.watchAdvertisements({ signal: abortController.signal }).catch(e => {
                if (e.name !== 'AbortError') {
                    console.log('PrinterManager: watchAdvertisements error:', e.message);
                    resolve(false);  // Resolve instead of reject to continue gracefully
                }
            });
        });
    },
    
    // Connect to a specific device
    async connectToDevice(device) {
        try {
            this.device = device;
            const server = await device.gatt.connect();
            
            // Try common thermal printer service UUIDs
            const serviceUUIDs = [
                '000018f0-0000-1000-8000-00805f9b34fb',
                '0000ff00-0000-1000-8000-00805f9b34fb',
                '49535343-fe7d-4ae5-8fa9-9fafd205e455',
                '0000ffe0-0000-1000-8000-00805f9b34fb'
            ];
            
            for (const uuid of serviceUUIDs) {
                try {
                    const service = await server.getPrimaryService(uuid);
                    const characteristics = await service.getCharacteristics();
                    
                    for (const char of characteristics) {
                        if (char.properties.write || char.properties.writeWithoutResponse) {
                            this.characteristic = char;
                            this.isConnected = true;
                            this.printerName = device.name;
                            this.printerId = device.id;  // Save the device ID (MAC address)
                            
                            // Save to database with ID
                            await this.savePrinterStatus(device.name, device.id);
                            
                            console.log('PrinterManager: Connected to', device.name, 'ID:', device.id);
                            this.updateStatusUI('connected');
                            this.showNotification('Printer terhubung: ' + device.name, 'success');
                            
                            // Handle disconnection
                            device.addEventListener('gattserverdisconnected', () => {
                                this.onDisconnected();
                            });
                            
                            // Process any pending receipts with longer delay and retry
                            // Wait for connection to stabilize before processing queue
                            this.schedulePendingQueueProcessing();
                            
                            return true;
                        }
                    }
                } catch (e) {
                    continue;
                }
            }
            
            throw new Error('No writable characteristic found');
            
        } catch (error) {
            console.error('PrinterManager: Connection error:', error);
            this.isConnected = false;
            this.updateStatusUI('lastknown');
            return false;
        }
    },
    
    // Handle disconnection
    onDisconnected() {
        console.log('PrinterManager: Disconnected');
        this.isConnected = false;
        this.characteristic = null;
        
        // Track disconnect for exponential backoff
        const now = Date.now();
        if (now - this.lastDisconnectTime < 60000) {  // Within 1 minute
            this.disconnectCount++;
        } else {
            this.disconnectCount = 1;  // Reset if it's been more than a minute
        }
        this.lastDisconnectTime = now;
        
        // Apply cooldown based on disconnect frequency (exponential backoff)
        const cooldownMs = Math.min(5000 * Math.pow(2, this.disconnectCount - 1), 60000);  // Max 60 seconds
        
        this.reconnectAttempts = 0;  // Reset attempts for new reconnect cycle
        this.updateStatusUI('disconnected');
        this.showNotification('Printer terputus - akan mencoba menghubungkan ulang...', 'warning');
        
        // Restart reconnect checker with cooldown
        setTimeout(() => {
            this.startReconnectChecker();
        }, cooldownMs);
        
        console.log(`PrinterManager: Will retry in ${cooldownMs/1000}s (disconnect count: ${this.disconnectCount})`);
    },
    
    // Connect to new printer (user initiated)
    async connect() {
        if (!navigator.bluetooth) {
            this.showNotification('Bluetooth tidak didukung di browser ini', 'error');
            return false;
        }
        
        try {
            this.updateStatusUI('connecting');
            
            const device = await navigator.bluetooth.requestDevice({
                acceptAllDevices: true,
                optionalServices: [
                    '000018f0-0000-1000-8000-00805f9b34fb',
                    '0000ff00-0000-1000-8000-00805f9b34fb',
                    '49535343-fe7d-4ae5-8fa9-9fafd205e455',
                    '0000ffe0-0000-1000-8000-00805f9b34fb'
                ]
            });
            
            return await this.connectToDevice(device);
            
        } catch (error) {
            console.error('PrinterManager: Connect error:', error);
            this.isConnected = false;
            this.updateStatusUI('disconnected');
            
            if (error.name !== 'NotFoundError') {
                this.showNotification('Gagal menghubungkan printer: ' + error.message, 'error');
            }
            return false;
        }
    },
    
    // Disconnect printer
    async disconnect() {
        if (this.device && this.device.gatt.connected) {
            this.device.gatt.disconnect();
        }
        this.isConnected = false;
        this.characteristic = null;
        this.printerName = null;
        this.printerId = null;
        
        // Clear from database
        await this.savePrinterStatus('', '');
        
        this.updateStatusUI('disconnected');
        this.showNotification('Printer diputuskan', 'info');
    },
    
    // Save printer status to database (with device ID)
    async savePrinterStatus(printerName, printerId = null) {
        try {
            await fetch('/api/printer-status', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    printer_name: printerName,
                    printer_id: printerId
                })
            });
        } catch (error) {
            console.error('PrinterManager: Error saving printer status:', error);
        }
    },
    
    // Print raw byte data
    async printRaw(data) {
        if (!this.isConnected || !this.characteristic) {
            throw new Error('Printer tidak terhubung');
        }
        
        // Send in chunks of 100 bytes
        const chunkSize = 100;
        for (let i = 0; i < data.length; i += chunkSize) {
            const chunk = data.slice(i, i + chunkSize);
            if (this.characteristic.properties.writeWithoutResponse) {
                await this.characteristic.writeValueWithoutResponse(new Uint8Array(chunk));
            } else {
                await this.characteristic.writeValue(new Uint8Array(chunk));
            }
            await new Promise(resolve => setTimeout(resolve, 50));
        }
        
        return true;
    },
    
    // Print text data
    async print(data) {
        if (!this.isConnected || !this.characteristic) {
            this.showNotification('Printer tidak terhubung', 'error');
            return false;
        }
        
        try {
            const encoder = new TextEncoder();
            const bytes = encoder.encode(data);
            await this.printRaw(Array.from(bytes));
            return true;
        } catch (error) {
            console.error('PrinterManager: Print error:', error);
            this.showNotification('Gagal mencetak: ' + error.message, 'error');
            return false;
        }
    },
    
    // Print receipt with auto-connect and pending queue support
    async printReceipt(receiptCommands, addToQueueOnFail = true) {
        // First check if connected
        if (!this.isConnected) {
            console.log('PrinterManager: Not connected, attempting to connect...');
            
            // Try to auto-reconnect
            const connected = await this.autoReconnect();
            
            if (!connected) {
                // If still not connected, add to pending queue
                if (addToQueueOnFail) {
                    this.addToPendingQueue(receiptCommands);
                }
                return false;
            }
        }
        
        // Now try to print
        try {
            await this.printRaw(receiptCommands);
            this.showNotification('Struk berhasil dicetak!', 'success');
            return true;
        } catch (error) {
            console.error('PrinterManager: Print error:', error);
            
            // If print failed, add to queue
            if (addToQueueOnFail) {
                this.addToPendingQueue(receiptCommands);
            }
            return false;
        }
    },
    
    // Build receipt commands from order data
    buildReceiptCommands(order, copyNum = 1, totalCopies = 1) {
        const encoder = new TextEncoder();
        let commands = [];
        
        const copyLabels = ['Kasir', 'Pelanggan', 'Dapur'];
        const copyLabel = copyLabels[copyNum - 1] || `Copy ${copyNum}`;
        
        // Initialize printer
        commands.push(...this.ESC_POS.INIT);
        
        // Header
        commands.push(...this.ESC_POS.ALIGN_CENTER);
        commands.push(...this.ESC_POS.BOLD_ON);
        commands.push(...encoder.encode('================================\n'));
        commands.push(...encoder.encode(' ★ DAPOER TERAS OBOR ★\n'));
        commands.push(...encoder.encode('    Kuliner Nusantara\n'));
        commands.push(...encoder.encode('================================\n'));
        commands.push(...this.ESC_POS.BOLD_OFF);
        commands.push(...encoder.encode('Jl. Rw. Belong, Jakarta Barat\n'));
        commands.push(...encoder.encode('Telp: 021-XXXXXXX\n\n'));
        
        // Copy indicator
        commands.push(...this.ESC_POS.BOLD_ON);
        commands.push(...encoder.encode(`>>> Lembar ${copyNum}/${totalCopies} - ${copyLabel} <<<\n`));
        commands.push(...this.ESC_POS.BOLD_OFF);
        commands.push(...encoder.encode('--------------------------------\n'));
        
        // Order info
        commands.push(...this.ESC_POS.ALIGN_LEFT);
        commands.push(...encoder.encode(`No Order : ${order.order_number || 'N/A'}\n`));
        commands.push(...encoder.encode(`Tanggal  : ${new Date().toLocaleDateString('id-ID')}\n`));
        commands.push(...encoder.encode(`Jam      : ${new Date().toLocaleTimeString('id-ID')}\n`));
        if (order.table) {
            commands.push(...encoder.encode(`Meja     : ${order.table}\n`));
        }
        if (order.customer_name) {
            commands.push(...encoder.encode(`Nama     : ${order.customer_name}\n`));
        }
        
        commands.push(...encoder.encode('--------------------------------\n'));
        commands.push(...this.ESC_POS.ALIGN_CENTER);
        commands.push(...this.ESC_POS.BOLD_ON);
        commands.push(...encoder.encode('DETAIL PESANAN\n'));
        commands.push(...this.ESC_POS.BOLD_OFF);
        commands.push(...encoder.encode('--------------------------------\n'));
        commands.push(...this.ESC_POS.ALIGN_LEFT);
        
        // Items
        if (order.items && order.items.length > 0) {
            for (const item of order.items) {
                const name = (item.name || '').substring(0, 20);
                const qty = `x${item.quantity}`;
                const price = this.formatNumber(item.subtotal);
                
                commands.push(...encoder.encode(`${name}\n`));
                commands.push(...encoder.encode(`   ${qty}           Rp ${price}\n`));
            }
        }
        
        commands.push(...encoder.encode('--------------------------------\n'));
        
        // Totals
        const pad = (label, value) => {
            const space = 32 - label.length - value.length;
            return label + ' '.repeat(Math.max(1, space)) + value;
        };
        
        commands.push(...encoder.encode(pad('Subtotal:', `Rp ${this.formatNumber(order.subtotal || 0)}\n`)));
        if (order.discount && order.discount > 0) {
            commands.push(...encoder.encode(pad('Diskon:', `- Rp ${this.formatNumber(order.discount)}\n`)));
        }
        commands.push(...encoder.encode(pad('Pajak (10%):', `Rp ${this.formatNumber(order.tax || 0)}\n`)));
        
        commands.push(...encoder.encode('================================\n'));
        commands.push(...this.ESC_POS.BOLD_ON);
        commands.push(...this.ESC_POS.DOUBLE_HEIGHT);
        commands.push(...encoder.encode(pad('TOTAL:', `Rp ${this.formatNumber(order.total || 0)}\n`)));
        commands.push(...this.ESC_POS.NORMAL_SIZE);
        commands.push(...this.ESC_POS.BOLD_OFF);
        commands.push(...encoder.encode('================================\n'));
        
        // Payment info
        if (order.payment) {
            commands.push(...encoder.encode(pad('Bayar:', `Rp ${this.formatNumber(order.payment.paid_amount || 0)}\n`)));
            commands.push(...encoder.encode(pad('Kembalian:', `Rp ${this.formatNumber(order.payment.change_amount || 0)}\n`)));
            commands.push(...encoder.encode(`Metode: ${order.payment.payment_method || 'Cash'}\n`));
        }
        
        commands.push(...encoder.encode('--------------------------------\n'));
        
        // Footer
        commands.push(...this.ESC_POS.ALIGN_CENTER);
        commands.push(...this.ESC_POS.BOLD_ON);
        commands.push(...encoder.encode('\n★ TERIMA KASIH ★\n'));
        commands.push(...this.ESC_POS.BOLD_OFF);
        commands.push(...encoder.encode('Atas Kunjungan Anda\n'));
        commands.push(...encoder.encode('Selamat Menikmati Hidangan\n\n'));
        commands.push(...encoder.encode('~ Dapoer Teras Obor ~\n'));
        commands.push(...encoder.encode(`Lembar ${copyNum}/${totalCopies} - ${copyLabel}\n`));
        commands.push(...encoder.encode('================================\n'));
        commands.push(...this.ESC_POS.FEED_LINES(4));
        
        // Cut paper
        commands.push(...this.ESC_POS.CUT_PAPER);
        
        return commands;
    },
    
    // Format number with thousands separator
    formatNumber(num) {
        return new Intl.NumberFormat('id-ID').format(num || 0);
    },
    
    // Auto-print receipt with 3 copies (for successful payment)
    async autoPrintReceipt(order, copies = 3) {
        console.log('PrinterManager: Auto-printing receipt for order', order.order_number);
        
        for (let i = 1; i <= copies; i++) {
            const commands = this.buildReceiptCommands(order, i, copies);
            const success = await this.printReceipt(commands);
            
            if (!success && i === 1) {
                // If first print fails, the rest will be in queue too
                // Add remaining copies to queue
                for (let j = i + 1; j <= copies; j++) {
                    const remainingCommands = this.buildReceiptCommands(order, j, copies);
                    this.addToPendingQueue(remainingCommands);
                }
                break;
            }
            
            // Delay between prints
            if (i < copies && success) {
                await new Promise(resolve => setTimeout(resolve, 800));
            }
        }
    },
    
    // Update UI status indicator
    updateStatusUI(status) {
        const statusEl = document.getElementById('printer-status');
        const statusText = document.getElementById('printer-status-text');
        const statusDot = document.getElementById('printer-status-dot');
        const actionBtn = document.getElementById('printer-action-btn');
        const autoStatus = document.getElementById('printer-auto-status');
        
        // Update pending count indicator if exists
        const pendingCount = document.getElementById('printer-pending-count');
        if (pendingCount) {
            const count = this.getPendingCount();
            if (count > 0) {
                pendingCount.textContent = count;
                pendingCount.classList.remove('hidden');
            } else {
                pendingCount.classList.add('hidden');
            }
        }
        
        if (!statusEl) return;
        
        statusEl.classList.remove('hidden');
        
        const currentStatus = status || (this.isConnected ? 'connected' : 'disconnected');
        
        switch (currentStatus) {
            case 'connected':
                statusDot.className = 'w-2 h-2 rounded-full bg-green-500';
                statusText.textContent = this.printerName || 'Terhubung';
                statusText.className = 'text-xs text-green-600 truncate max-w-24';
                if (actionBtn) {
                    actionBtn.textContent = 'Putuskan';
                    actionBtn.className = 'text-xs px-2 py-1 rounded bg-red-500/20 text-red-400 hover:bg-red-500/30 transition-colors';
                }
                if (autoStatus) autoStatus.classList.add('hidden');
                break;
            case 'connecting':
                statusDot.className = 'w-2 h-2 rounded-full bg-yellow-500 animate-pulse';
                statusText.textContent = 'Menghubungkan...';
                statusText.className = 'text-xs text-yellow-600 truncate max-w-24';
                if (actionBtn) {
                    actionBtn.textContent = 'Menunggu...';
                    actionBtn.className = 'text-xs px-2 py-1 rounded bg-yellow-500/20 text-yellow-400';
                }
                if (autoStatus) {
                    autoStatus.textContent = `Auto-reconnect... (${this.reconnectAttempts}/${this.maxReconnectAttempts})`;
                    autoStatus.classList.remove('hidden');
                }
                break;
            case 'lastknown':
                statusDot.className = 'w-2 h-2 rounded-full bg-yellow-500';
                statusText.textContent = this.printerName || 'Terakhir terhubung';
                statusText.className = 'text-xs text-yellow-600 truncate max-w-24';
                if (actionBtn) {
                    actionBtn.textContent = 'Hubungkan';
                    actionBtn.className = 'text-xs px-2 py-1 rounded bg-green-500/20 text-green-400 hover:bg-green-500/30 transition-colors';
                }
                if (autoStatus) {
                    if (!this.autoReconnectSupported) {
                        // Show message that manual connection is required
                        autoStatus.textContent = 'Klik untuk hubungkan manual';
                        autoStatus.classList.remove('hidden');
                    } else if (this.reconnectAttempts < this.maxReconnectAttempts) {
                        autoStatus.textContent = 'Auto-reconnect aktif...';
                        autoStatus.classList.remove('hidden');
                    } else {
                        autoStatus.textContent = 'Klik tombol untuk hubungkan manual';
                        autoStatus.classList.remove('hidden');
                    }
                }
                break;
            case 'disconnected':
            default:
                statusDot.className = 'w-2 h-2 rounded-full bg-gray-400';
                statusText.textContent = 'Tidak terhubung';
                statusText.className = 'text-xs text-gray-500 truncate max-w-24';
                if (actionBtn) {
                    actionBtn.textContent = 'Hubungkan';
                    actionBtn.className = 'text-xs px-2 py-1 rounded bg-white/10 hover:bg-white/20 transition-colors';
                }
                if (autoStatus) autoStatus.classList.add('hidden');
                break;
        }
    },
    
    // Show notification toast
    showNotification(message, type = 'info') {
        // Create toast element
        const toast = document.createElement('div');
        toast.className = `fixed top-4 right-4 px-4 py-2 rounded-lg shadow-lg z-50 transition-all duration-300 transform translate-x-full`;
        
        switch (type) {
            case 'success':
                toast.classList.add('bg-green-500', 'text-white');
                break;
            case 'error':
                toast.classList.add('bg-red-500', 'text-white');
                break;
            case 'warning':
                toast.classList.add('bg-yellow-500', 'text-white');
                break;
            default:
                toast.classList.add('bg-blue-500', 'text-white');
        }
        
        toast.innerHTML = `
            <div class="flex items-center gap-2">
                <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 17h2a2 2 0 002-2v-4a2 2 0 00-2-2H5a2 2 0 00-2 2v4a2 2 0 002 2h2m2 4h6a2 2 0 002-2v-4a2 2 0 00-2-2H9a2 2 0 00-2 2v4a2 2 0 002 2zm8-12V5a2 2 0 00-2-2H9a2 2 0 00-2 2v4h10z"/>
                </svg>
                <span>${message}</span>
            </div>
        `;
        
        document.body.appendChild(toast);
        
        // Animate in
        setTimeout(() => {
            toast.classList.remove('translate-x-full');
        }, 10);
        
        // Remove after 3 seconds
        setTimeout(() => {
            toast.classList.add('translate-x-full');
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => {
    PrinterManager.init();
});

// Also try to reconnect when page becomes visible (user returns to tab)
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !PrinterManager.isConnected && (PrinterManager.printerName || PrinterManager.printerId)) {
        console.log('PrinterManager: Page visible, attempting reconnect...');
        PrinterManager.reconnectAttempts = 0;
        if (PrinterManager.autoReconnectSupported) {
            PrinterManager.autoReconnect();
        }
    }
});

// Handle page focus to attempt reconnection (more aggressive than visibility)
window.addEventListener('focus', () => {
    if (!PrinterManager.isConnected && (PrinterManager.printerName || PrinterManager.printerId) && PrinterManager.autoReconnectSupported) {
        const now = Date.now();
        // Debounce: only reconnect if enough time has passed since last focus attempt
        if (now - PrinterManager.lastFocusReconnectTime > PrinterManager.CONFIG.FOCUS_DEBOUNCE_MS) {
            console.log('PrinterManager: Window focused, checking connection...');
            PrinterManager.lastFocusReconnectTime = now;
            // Small delay to avoid rapid reconnection attempts
            setTimeout(() => {
                if (!PrinterManager.isConnected) {
                    PrinterManager.reconnectAttempts = 0;
                    PrinterManager.autoReconnect();
                }
            }, PrinterManager.CONFIG.BASE_RETRY_DELAY_MS);
        }
    }
});

// Handle page unload to clean up
window.addEventListener('beforeunload', () => {
    PrinterManager.stopReconnectChecker();
});

// Expose PrinterManager globally
window.PrinterManager = PrinterManager;
