export class AccountBrokerError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AccountBrokerError";
  }
}

export class AccountUnavailable extends AccountBrokerError {
  constructor(message: string) {
    super(message);
    this.name = "AccountUnavailable";
  }
}

export class AccountSchemaError extends AccountBrokerError {
  constructor(message: string) {
    super(message);
    this.name = "AccountSchemaError";
  }
}

export class AccountBusy extends AccountBrokerError {
  constructor(message: string) {
    super(message);
    this.name = "AccountBusy";
  }
}
